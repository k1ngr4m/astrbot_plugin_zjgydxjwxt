# astrbot_plugin_zjgydxjwxt

浙江工业大学研究生服务接口结果查询插件。

## 指令

- `/绑定datas <datas>`：绑定当前用户的 datas。
- `/结果查询`：使用当前用户已绑定的 datas 查询结果。
- `/结果查询 <datas>`：临时使用指定 datas 查询（不覆盖绑定值）。
- `/自动查询`：立即执行一轮自动查询，并返回本轮统计。

## 行为说明

- 调用接口：`https://yjsfw.zjut.edu.cn/gsapp/sys/wdxwxxapp/modules/xsbdjcsq/queryPyjg.do`
- 请求方式：`POST`（`application/x-www-form-urlencoded`）
- 请求体：`datas=<输入参数>`
- 当接口返回：
  `{"fwpjgList":[],"pyjgList":[],"success":true}`
  时，机器人回复：`暂无结果。`
- `datas` 按用户隔离保存到插件目录下 `datas_bindings.json`。
- 自动查询默认每 10 分钟执行一次，仅在北京时间（Asia/Shanghai）时间窗口内生效（默认 `08:00-22:00`，可配置）。
- 非空且与上次推送结果不同才会自动推送，避免重复刷屏。

## 配置

在插件配置中设置：

- `cookie`：必填，请填浏览器抓取到的完整 Cookie 字符串。
- `base_headers`：可选，字典，用于覆盖/补充默认请求头。
- `timeout`：可选，请求超时时间（秒），默认 `15`。
- `auto_query_enabled`：可选，是否开启自动查询，默认 `true`。
- `auto_query_interval_minutes`：可选，自动查询间隔（分钟），默认 `10`。
- `auto_query_window_start`：可选，自动查询开始时间（北京时间，`HH:MM`），默认 `08:00`。
- `auto_query_window_end`：可选，自动查询结束时间（北京时间，`HH:MM`），默认 `22:00`。

示例：

```yaml
cookie: "_ht=person; GS_SESSIONID=xxxx; JSESSIONID=xxxx"
timeout: 15
auto_query_enabled: true
auto_query_interval_minutes: 10
auto_query_window_start: "08:00"
auto_query_window_end: "22:00"
base_headers:
  Referer: "https://yjsfw.zjut.edu.cn/gsapp/sys/wdxwxxapp/*default/index.do?THEME=indigo&EMAP_LANG=zh"
```
