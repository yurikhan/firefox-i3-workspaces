# Restore Firefox windows to correct i3 workspaces

## Problem statement

I use Firefox with multiple windows,
distributed over several i3 workspaces.
I have Firefox set up to restore windows and tabs of the last session.
I want to rely on each window staying on the workspace I move it to,
including across Firefox and PC restarts.

The code in Firefox that is supposed to restore windows
does not work on i3 and is specifically disabled if i3 is detected,
due to the way i3 implements the relevant X window hints.

Thus, when Firefox restarts, it dumps all my windows on the workspace
that is active at the time.

## Approach

This repo contains a Firefox extension and a helper Python script
that cooperate to associate an i3 workspace name to each Firefox window
and move each window to its workspace after session restore.

## Installation

0. Install the dependencies.
   The host app needs Python 3.10 and the `i3ipc` library
   which can be installed on Debian derivatives
   with `sudo apt install python3-i3ipc`.
   For other distributions, check your package manager,
   or install via `pip`.
1. Clone the repo in a directory of your choice.
2. Read the `host/i3_workspaces.py` script
   to convince yourself it is not malicious
   and does not receive, collect, store or process
   any of your sensitive information.
   (Spoiler: it is not and does not,
   but this is basic information hygiene
   when installing and running scripts
   written by random strangers on the Internet.)
3. Copy the file `host/i3_workspaces.json`
   to your `~/.mozilla/native-messaging-hosts` directory.
   Change the `path` value to reflect the actual installation location.
   (On Linux, it must be an absolute path.)
4. Install the add-on,
   either from the Releases section on GitHub
   or from addons.mozilla.org.

----

## Theory of operation

### Firefox window identification

Internally, Firefox identifies windows with small integers.
These are stable while Firefox is running
but change across Firefox restart.

The addon tacks a UUID to each window
using the `sessions.setWindowValue` API.
These are unique and stable,
and persist across restarts until the addon is uninstalled.

The addon enumerates all windows at addon startup,
which happens soon after the windows are restored at Firefox startup.
If there’s any window without a UUID, the addon assigns one.

Additionally, the addon subscribes to the `windows.onCreated` event.
This covers the cases when I
create new windows,
detach tabs from an existing window,
or restore a recently closed window.

This way, at all times,
the addon keeps an up-to-date mapping of UUIDs to window IDs.

### Workspace assignment persistence

The addon stores the name of the i3 workspace
each window was last seen on
using `sessions.setWindowValue`.
As said above, these values persist across restarts.

### i3 communication

The addon uses native messaging to communicate to a host app.
The host app is written in Python
and talks to i3 on the addon’s behalf using the i3ipc library.

An i3 IPC connection dies if i3 is restarted.
When that happens, the host app reconnects
and resubscribes to events it tracks.

#### Window placement

At startup and on any new window creation,
the addon modifies each window’s title,
using `windows.update` with the `titlePreface` property,
to contain the window UUID,
and sends the host app a message
with a mapping of window UUIDs to workspace names.

```
→ { "windows": {
      "0748cda1-ba4d-475d-bae6-3d1f58113fd1": "2",
      "c1df378e-b501-4e57-aa7a-142688f297e4": null
  } }
```

The host app asks i3 for the current layout tree,
finds each window in the addon’s request by the UUID in its title,
and correlates X window IDs to UUIDs.
X window IDs are stable within an X session
and survive i3 restarts.
If a workspace name is passed by the addon,
the host app moves the window to that workspace.
It sends the name of the workspace each window ended up on
back to the addon.

```
← { "windows": {
      "0748cda1-ba4d-475d-bae6-3d1f58113fd1": "2",
      "c1df378e-b501-4e57-aa7a-142688f297e4": "3"
  } }
```

Upon receiving this response from the host app,
the addon finds each window mentioned by its UUID,
updates its last known workspace,
and resets the title prefix to an empty string.

#### Windows moving between workspaces

At startup and on each i3 reconnection,
the host app subscribes to the i3 `window::move` event.
When that event occurs on a window
whose ID is known to be a Firefox window,
the host app will send a message to the addon,
with the UUID and new workspace name.

```
← { "window::move": {
      "0748cda1-ba4d-475d-bae6-3d1f58113fd1": "1"
  } }
```

Receiving this message,
the addon updates the window’s last known workspace name.

#### Workspaces being renamed

At startup and on each i3 reconnection,
the host app gets all existing workspaces
and builds a map from i3 container ID to name.

It also subscribes to the `workspace::rename` event.
When that event occurs, it updates its map
and sends the old and new names to the addon.

```
← { "workspace::rename": {
      "1": "1 foo"
  } }
```

In response to this message, the addon enumerates all windows
and updates the workspace value if it matches the old name.
