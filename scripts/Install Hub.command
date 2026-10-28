#!/bin/sh
# Explicit installation, separate from the connect-only Hub window.
set -eu

fail() {
    printf '%s\n' "$1" >&2
    if [ -t 0 ]; then
        printf '按回车关闭此窗口。\n' >&2
        IFS= read -r ignored || :
    fi
    exit "$2"
}

if [ "$#" -ne 0 ]; then
    fail '此入口仅安装 Hub；其他操作请使用 Manage Hub Service.command。' 2
fi

app="/Applications/TaskPaw Hub.app"
if [ ! -d "$app" ]; then
    app="$HOME/Applications/TaskPaw Hub.app"
fi
backend="$app/Contents/MacOS/taskpaw-backend"
if [ ! -x "$backend" ]; then
    fail '请先将 TaskPaw Hub.app 拖入 Applications（或 ~/Applications），再双击 Install Hub.command。' 1
fi

# #357: the ad-hoc package is not notarized. Clearing the quarantine of this
# installed app only (after the operator approved this command) keeps macOS from
# asking again for its backend and window.
if ! xattr -drs com.apple.quarantine "$app"; then
    printf '未能清除 %s 的隔离标记；macOS 可能仍会要求在「系统设置 → 隐私与安全性」中点「仍要打开」。\n' "$app" >&2
fi

printf '正在安装并启动 Hub 后台服务，请稍候……\n'
if "$backend" hub-service install; then
    printf 'Hub 后台服务已就绪，正在打开监控界面。\n'
else
    code=$?
    fail 'Hub 后台服务安装失败，未打开监控界面。请保留上方错误，并查看 Manage Hub Service.command 的 status 或 HubService/hub.log。' "$code"
fi

if open "$app"; then
    printf '安装完成。关闭监控界面后，后台服务仍会继续运行。\n'
else
    code=$?
    fail '后台服务已安装，但无法打开 TaskPaw Hub.app；请从 Applications 手动打开。' "$code"
fi
