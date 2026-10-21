# 闲鱼「卖家填写演出信息」提交接口抓取工具链

## 为什么需要这个

演唱会门票订单在买家付款后，卖家要先在闲鱼 App 内**填写演出信息**（演出名称 / 门票数量 / 演出时间），买家确认后订单才进入待发货。这一步的提交接口是**写操作**，接口名与参数只在闲鱼 App 原生层（Flutter/Kraken + `libapp.so`），公开文档（淘宝开放平台）只有查询接口 `qimen.alibaba.idle.performance.query`，**没有任何提交接口文档**。

因此提交接口名只能二选一拿到：
- **A. 抓包**（手机跑闲鱼，抓它提交时的真实 mtop 调用）——本目录工具链就是干这个的。
- **B. 反编译闲鱼 APK 的 `libapp.so`**（同之前排查闲管家的方法）——把 APK 文件交给开发者即可，开发者在本地反编译 `strings` 搜 `mtop.taobao.idle.performance.*` 一类字样。

> ⚠️ 提交接口名**严禁硬编码猜测**：猜错会让写操作失败，且可能计入账号风控。

## 现在就能用的替代方案（无需抓包）

系统 v1.0.59 已内置 **APP 深链兜底**：点订单卡片的「填写演出信息」→ 弹窗引导打开
`fleamarket://smart_form?sceneCode=YCH&outerType=ORDER&outerId=订单号`
手机会跳到闲鱼原生填写页，订单号已带好，卖家填 3 个字段提交即可。
这是今天就能用的日常方案；本工具链的目标是把它升级成「系统内一键直提交」。

---

## 路径 A：抓包（推荐，最稳）

### 一次性准备
```bash
bash scripts/capture_xianyu/setup_capture.sh
```
- 会在 `scripts/capture_xianyu/.venv` 装好 `frida-tools`。
- 按脚本提示，下载与 frida 版本一致的 `frida-server`（Android arm64），推到手机并以 root 运行：
  ```bash
  adb push frida-server /data/local/tmp/
  adb shell 'chmod 755 /data/local/tmp/frida-server'
  adb shell 'su -c /data/local/tmp/frida-server &'
  adb shell 'su -c frida-ps -U'   # 能列出进程即成功
  ```

### 抓包
```bash
bash scripts/capture_xianyu/run_capture.sh
```
1. 脚本拉起闲鱼（spawn 注入）。
2. 在 App 内走一次：门票订单 → 我填写演出信息 → 填完提交。
3. 终端会打印 `[MTOP-REQ] api=...` 以及带 `concertName/ticketCount/date` 的请求/响应报文。
4. `Ctrl+C` 结束，结果已存 `capture_out.txt`。
5. 把 `api=` 那行 + 报文 JSON 发给开发者，即可填进系统设置 `shipping.show_info_api`。

脚本已做：禁用闲鱼 SPDY 私有协议（否则抓到的是二进制帧）、mtop 请求层 hook、TLS 层兜底抓取、关键词过滤只留演出信息流量。

---

## 路径 B：反编译闲鱼 APK（更省事，只需交文件）

若你手头有闲鱼 APK（手机里 `/data/app/com.taobao.idlefish*/base.apk` 用 `adb pull` 取，
或从应用商店下载），把它交给开发者。开发者在本地：
```bash
unzip -o xianyu.apk -d xianyu_apk >/dev/null
strings xianyu_apk/lib/arm64-v8a/libapp.so | grep -iE 'performance|showinfo|smartform|YCH|concertName|ticketCount' | sort -u
```
即可定位提交接口名（如 `mtop.taobao.idle.trade.order.fillPerformanceInfo` 之类），
再比对抓包/卡片 schema 确认入参。这条路径你几乎零操作，只需把 APK 文件给出去。

---

## 拿到接口名之后

填进系统设置（管理员 → 系统设置 → 「演出信息提交接口（演唱会门票）」）：
- `shipping.show_info_api` = 抓到的 mtop 名（如 `mtop.taobao.idle.trade.order.fillPerformanceInfo`）
- `shipping.show_info_api_version` = 抓到的 `v`（通常 `1.0`）

保存后，`websocket/app/services/shipping/show_info_service.py` 的 `submit_show_info`
就会走真实接口直提交，前端「填写演出信息」表单变成系统内一键提交，不再退化为深链。
