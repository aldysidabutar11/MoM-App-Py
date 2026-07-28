"""Desktop shell: a pywebview/WebView2 window over static HTML, CSS and JS.

No Electron, no React/Svelte/Vue, no npm build pipeline, no CDN asset, no remote
font, no external script or stylesheet. The window loads the local backend over
loopback, so the UI and the API share an origin and no CORS configuration is
needed.
"""

from mom_igd.shell.launcher import ShellApi, manual_launch_command, run_shell

__all__ = ["ShellApi", "manual_launch_command", "run_shell"]
