# 完成事件 watcher

仅依赖 Python 3 标准库。路由配置复制 config.example.json 后自行填写环境变量名，真实环境变量值不写进文件。对账另外复制 reconcile.config.example.json，配置 aliases（旧名到正式名，同路由且禁止链式别名）和非空 required_agents。reconcile.py 必须传 --config 路由文件和 --policy 对账配置。

运行离线回归：`PYTHONDONTWRITEBYTECODE=1 python3 selftest.py`。测试禁止 socket，HTTP 使用注入的假发送器；file 模式只写测试临时目录。

HTTP 首次接入必须显式选择 --start-at-end 或 --allow-history；有 cursor 或末尾接入 pending 时拒绝 --allow-history。只有 HTTP 的显式历史扫描写历史选择日志。日常重启不带接入参数。

生产对账要求检查当天之前最近两个完整北京时间日，且必须满足事件、每助手、别名、旧回报和场景证据门槛；其他窗口只能 --diagnostic，不能生产放行。别名和必需助手名单全部由 --policy 读取。
