# frp — 轻薄本备用隧道（Tailscale 之外第二条路）

Tailscale 在校园网里老断（UDP 打洞被限，只能走 DERP 中继）。frp 是纯 TCP 的反向代理，
本子主动连阿里云，云 VM 再从阿里云连回本子。

## 结构

- frps：阿里云 ECS（StarDraw 那台，公网 IP 120.25.214.139），`bindPort = 7000`，
  systemd 服务 `frps`，已装好并在跑（2026-11-20，frp v0.72.0）。
- frpc：轻薄本 `C:\dafeiyu-open\frp\frpc.exe`，计划任务 `dafeiyu-frpc`（登录启动，
  失败自动重启），只穿一个端口：阿里云 `6022` → 本子 `22`（SSH）。
- token：两端同一个，存在本子 `C:\dafeiyu-open\api_keys.json` 的 `frp_token`，
  绝不入库；模板文件里只是占位符。

## 安全

- 只暴露 SSH（密钥登录），不穿 io-wechat 23392（它的 /send 无鉴权，绝不能上公网），
  不穿 AstrBot 6185。
- 阿里云安全组要放行入方向 7000（frpc 连 frps）和 6022（云 VM 连 SSH）。
  frps 本身有 token 鉴权，6022 上的 SSH 只认密钥。

## 用法（连本子）

```bash
ssh -p 6022 24525@120.25.214.139
```

Tailscale 正常时继续走 Tailscale；断了就走 frp。`ssh-laptop.sh`（待补）会自动先试
Tailscale 再试 frp。

**⚠️ 云 VM（htch-runtime）走不了 frp：**它的出口代理只放行 443 端口的 CONNECT，
直连 SSH 会被 198.19.0.1:3128 reset（2026-11-20 实测）。所以 VM 自己的访问仍靠
Tailscale；frp 是给用户（和任何正常网络的机器）用的备用入口——Tailscale 断了，
用户仍能从自己的电脑/手机经阿里云 SSH 进本子。frp 通道本身（本子↔阿里云）已
验证通：frpc 日志 `login to server success` + `proxy added: [laptop-ssh]`，
服务器上 6022 已监听。

## 重装

- 服务器：`sudo bash install-frps.sh <token>`（token 问本子 api_keys.json 要）。
- 本子：见 `install-frpc.ps1`（仓库外，本子本地有；逻辑：解压 frpc.exe → 从
  api_keys.json 取 token 写 frpc.toml → 注册 `dafeiyu-frpc` 计划任务 → 启动）。
