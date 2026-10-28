#!/bin/sh
# This command is deliberately separate from the connect-only Hub window.
set -eu
app="/Applications/TaskPaw Hub.app"
if [ ! -d "$app" ]; then
    app="$HOME/Applications/TaskPaw Hub.app"
fi
backend="$app/Contents/MacOS/taskpaw-backend"
if [ ! -x "$backend" ]; then
    echo 'First drag TaskPaw Hub.app into Applications (or ~/Applications).'
    exit 1
fi
if [ "$#" -eq 0 ]; then
    echo 'Hub background service: install | status | start | stop | restart | uninstall'
    echo 'install starts aggregation; uninstall preserves configuration and history.'
    printf 'Action (blank cancels): '
    IFS= read -r action
else
    action="$1"
fi
case "$action" in
    install|status|start|stop|restart|uninstall) ;;
    '') exit 0 ;;
    *) echo 'Unknown action.'; exit 1 ;;
esac
# #357: as Install Hub.command, so the backend is not blocked again
if ! xattr -dr com.apple.quarantine "$app"; then
    echo "Could not clear the quarantine of $app; macOS may ask in System Settings → Privacy & Security." >&2
fi
exec "$backend" hub-service "$action"
