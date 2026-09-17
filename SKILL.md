---
name: sharepoint-stream-video-download
description: 从 SharePoint / OneDrive 上的 Stream 视频页面（stream.aspx）下载视频文件。当页面提示无下载权限、直接下载返回 403/「拒绝访问」时，改用播放器的转码 DASH 流（抓分段 + AES-128 解密 + ffmpeg 合流）拿到完整视频。触发词：SharePoint 视频下载、Stream 下载、stream.aspx、会议录制下载、「下载这个视频但没权限」。
agent_created: true
---

# SharePoint / Stream 视频下载

从 `https://<tenant>-my.sharepoint.com/personal/.../_layouts/15/stream.aspx?id=...` 这类页面
把视频存成本地 mp4。

## 决策顺序（按顺序试，别跳步）

### 1. 先试官方直链（最快，需要下载权限）

```
https://<tenant>-my.sharepoint.com/personal/<user>/_layouts/15/download.aspx?SourceUrl=<URL-encoded 服务器相对路径>
```

同时可试 REST：
```
.../_api/web/GetFileByServerRelativeUrl(@p)/$value?@p='<encoded>'
.../_api/v2.1/drives/{driveId}/items/{itemId}/content     # 302 → tempauth 预授权地址
```

判断成功：响应 `Content-Type` 是 `video/*` 或 `application/octet-stream`，
并且带 `Content-Disposition: attachment`。

**失败特征**（说明账号只有查看权限，走第 2 步）：
- `download.aspx` 返回 HTML，里面有 `"isCurrentUserHasAccessToSource":false`；
- REST `$value` 返回 `403 ... UnauthorizedAccessException`；
- 302 出来的 tempauth 地址返回 `{"error":{"code":"accessDenied","message":"Access denied"}}`。

### 2. 走播放器转码流（查看权限就够）

用 [scripts/stream_grab.py](scripts/stream_grab.py)，它把整套流程都实现了：

```bash
<venv>/Scripts/python.exe scripts/stream_grab.py \
    --url "<stream.aspx 完整地址>" \
    --out "D:\path\to\video.mp4" \
    --profile "C:\Users\<you>\.agent-browser\profiles\cloak-sharepoint" \
    --ffmpeg "C:\tools\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
```

流程：CloakBrowser 起可见窗口 → 等用户手动登录（检测到 `FedAuth` cookie）→ 播放页面 →
抓 `x-spopactoken` 和 `videomanifest` 地址 → 解析 MPD → 取 AES 密钥 → 用**页面自身 fetch**
把全部分段 base64 搬回本地 → 解密 → 剥填充 → ffmpeg 合流。

## 必须知道的坑

1. **分段是 AES-128-CBC(SEA) 加密的**。密钥来自 MPD 的
   `ContentProtection/sea:CryptoPeriod@keyUriTemplate`（GET 回来是 **16 字节裸二进制**，
   不是 JSON）。IV 来自 `@IV`；实测**所有分段共用同一个 base IV**。
2. **每个分段解密后有 PKCS#7 填充字节**，必须用 box 遍历（`ftyp/moov/moof/styp/sidx/mdat/emsg`）
   找到最后一个完整 box 的结尾并截断。否则 fMP4 box 链被破坏，ffmpeg 会
   「Output file is empty, nothing was encoded」且 rc=0。
3. **绝对不要用 httpx / requests 批量拉分段**。同一个 cookie+token 上下文跑约 37 次后
   全部 401，且等 30s 也不恢复。而**页面自身的 `fetch(url, {credentials:'include'})`
   完全不受限**（实测 60/60 成功）。所以数据必须从浏览器里搬出来
   （`page.evaluate` 返回 base64，或 blob + download 事件）。
4. **`x-spopactoken` 只用于 `northeurope1-mediap.svc.ms` 的 videomanifest 请求**；
   分段请求走 `watchgas-my.sharepoint.com/_api_cached/...`，靠 cookie 鉴权。
5. **ffmpeg 是 Windows 程序**，传参必须用 `C:/...`；MSYS 的 `/c/...` 会
   报 "No such file or directory"。
6. **`page.evaluate` 对赋值语句会当成函数调用**：要写成
   `() => { window.__fn = async (...) => {...}; }` 这种箭头函数包装。

## 环境依赖

- CloakBrowser（隐身 Chromium，绕过反爬/指纹检测）
  `python -m venv <venv> && <venv>/Scripts/pip install cloakbrowser playwright`
  首次运行会自动下载浏览器二进制（约 200MB，v146 免费无需密钥）。
- ffmpeg / ffprobe（合流与校验）。
- 登录用可见窗口，需要用户手动完成 MSA 登录 + MFA。

## 环境限制（WorkBuddy 沙箱）

- **agent-browser 的守护进程不能跨 Bash 调用存活**——每个 Bash 命令结束后 daemon 被回收，
  所以多步「打开→等登录→再操作」不可行。改用 Python 脚本在**后台常驻任务**里跑完整流程。
- **命令结束后启动的 GUI 进程会被沙箱回收**。浏览器必须在后台常驻任务内启动。
- 会话会随进程结束而丢失，因此**登录态要落到磁盘**（`launch_persistent_context(user_data_dir=...)`
  或 `storage_state`），并在同一次运行内完成全部下载。

## 校验产物

```bash
ffprobe -v error -show_entries format=duration,size:stream=codec_name,width,height -of json out.mp4
ffmpeg -v error -i out.mp4 -t 90 -f null -      # 首段解码
ffmpeg -v error -ss <duration-120> -i out.mp4 -f null -   # 尾段解码
```

对比 MPD 的 `mediaPresentationDuration` 和 `/content` 元数据里的 `size`（原始文件字节数），
应基本吻合（本例 272,187,260 → 272,083,196，差值主要是 box 头部差异）。
