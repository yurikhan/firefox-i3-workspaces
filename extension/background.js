/// @typedef {string} UUID
/// @typedef {number} WindowID
/// @typedef {string} Workspace

/// @type {browser.runtime.Port}
let host = browser.runtime.connectNative('i3_workspaces');

/// @type {Map<UUID, WindowID>}
let windowMap = new Map();

/// For each window in ws, make sure it has a UUID.
///
/// @param {Array<browser.windows.Window>} ws
/// @return {Promise<Map<UUID, WindowID>>}
///
async function makeWindowMap(ws) {
  return new Map(await Promise.all(ws.map(async (w) => {
    let uuid = await browser.sessions.getWindowValue(w.id, 'uuid');
    if (uuid === undefined) {
      uuid = self.crypto.randomUUID();
      await browser.sessions.setWindowValue(w.id, 'uuid', uuid);
    }
    return [uuid, w.id];
  })));
}

/// Merge wmap into windowMap.
///
/// @param {Map<UUID, WindowID>} wmap
///
function updateWindowMap(wmap) {
  windowMap = new Map([...windowMap, ...wmap]);
}

/// @typedef {Object} WindowsMessage
/// @property {WindowsPayload} window
///
/// @typedef {Object<UUID, Workspace>} WindowsPayload

/// Build a message to ask the host, for each window in byUUID,
/// to move it to a specific workspace
/// or to tell the workspace it’s currently on.
///
/// @param {Map<UUID, WindowID>} byUUID
/// @return {Promise<WindowsMessage>}
///
async function makeWindowsMessage(byUUID) {
  return { windows: Object.fromEntries(await Promise.all([...byUUID].map(async ([uuid, id]) => [
    uuid,
    (await browser.sessions.getWindowValue(id, 'workspace')) ?? null,
  ])))};
}

/// Make each window’s title distinct enough for the host app to make note of it.
///
/// @param {Map<UUID, WindowID>} byUUID
/// @return {Array<Promise>} for each window
///
function beaconifyWindows(byUUID) {
  return [...byUUID].map(([uuid, id]) =>
    browser.windows.update(id, { titlePreface: `${uuid} | ` }));
}

/// Remove the distinctive title prefix for each window in windows.
///
/// @param {Object<UUID, Workspace>} windows
/// @return {Array<Promise>} for each window
///
function unbeaconifyWindows(windows) {
  return Object.keys(windows).map((uuid) =>
    browser.windows.update(windowMap.get(uuid), { titlePreface: '' }));
}

/// Update each window’s last known workspace.
///
/// @param {WindowsPayload} payload
/// @return {Array<Promise>} for each window
///
function windowsMoved(payload) {
  return Object.entries(payload).map(([uuid, workspace]) =>
    browser.sessions.setWindowValue(windowMap.get(uuid), 'workspace', workspace));
}

/// @typedef {Object<Workspace, Workspace>} RenamePayload

/// Update each window’s last known workspace in response to a renaming.
///
/// @param {RenamePayload} payload
/// @return {Promise<undefined>}
///
async function workspaceRenamed(payload) {
  const ws = await browser.windows.getAll();
  await Promise.all(ws.map(async (w) => {
    oldWorkspace = await browser.sessions.getWindowValue(w.id, 'workspace');
    newWorkspace = payload[oldWorkspace];
    if (newWorkspace !== undefined) {
      await browser.sessions.setWindowValue(w.id, 'workspace', newWorkspace);
    }
  }));
}

/// @typedef {Object} WindowsNotification
/// @property {WindowsPayload} windows
///
/// @typedef {Object} WindowMoveNotification
/// @property {WindowsPayload} window::move
///
/// @typedef {Object} WorkspaceRenameNotification
/// @property {RenamePayload} workspace::rename
///
/// @typedef {WindowsNotification | WindowMoveNotification | WorkspaceRenameNotification} Notification

/// Handle incoming messages from the host.
///
/// @param {Notification} message
/// @return {Promise<undefined>}
///
async function handleHostMessage(message) {
  console.log('←', message);
  if (message.windows) {
    await Promise.all([
      ...unbeaconifyWindows(message.windows),
      ...windowsMoved(message.windows),
    ]);
  } else if (message['window::move']) {
    await Promise.all(windowsMoved(message['window::move']));
  } else if (message['workspace::rename']) {
    await workspaceRenamed(message['workspace::rename']);
  }
}
host.onMessage.addListener(handleHostMessage);

/// Handle window creation.
///
/// @param {browser.windows.Window} w
/// @return {Promise<undefined>}
///
async function handleWindowCreated(w) {
  const wmap = await makeWindowMap([w]);
  updateWindowMap(wmap);

  const [message, ..._] = await Promise.all([
    makeWindowsMessage(wmap),
    ...beaconifyWindows(wmap),
  ]);
  console.log('→', message);
  host.postMessage(message);
}
browser.windows.onCreated.addListener(handleWindowCreated);

/// Handle addon startup
async function main() {
  const ws = await browser.windows.getAll();
  const wmap = await makeWindowMap(ws);
  updateWindowMap(wmap);

  const [message, ..._] = await Promise.all([
    makeWindowsMessage(windowMap),
    ...beaconifyWindows(windowMap),
  ]);
  console.log('→', message);
  host.postMessage(message);
}
main();
