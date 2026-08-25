#!/bin/bash
set -eu

service="codex-bark-push-url"
account="${USER:?无法读取当前 macOS 用户名}"

printf '请粘贴 Bark 首页显示的个人推送地址（输入不会显示）： '
IFS= read -r -s bark_url
printf '\n'

case "$bark_url" in
  http://*|https://*) ;;
  *)
    printf '地址格式不正确，未保存。\n' >&2
    exit 1
    ;;
esac

/usr/bin/security add-generic-password -U -a "$account" -s "$service" -w "$bark_url" >/dev/null
unset bark_url
printf '已保存到 Mac 钥匙串：%s\n' "$service"
