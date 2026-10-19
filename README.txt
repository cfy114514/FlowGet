FlowGet 0.1.0 — IDM 行为规格驱动的独立实现，第一阶段及第二阶段基础
2026-10-19

使用
  双击 FlowGet.exe，无需安装 Python。当前验证环境为 Windows 11 x64。
  新建下载 / Ctrl+N：粘贴 HTTP 或 HTTPS 直链，选择目录后加入下载队列。
  Ctrl+V：在主界面粘贴剪贴板中的下载链接，不会自动读取剪贴板。
  选择任务后可暂停、继续，双击查看属性。右键可复制链接或移除记录。
  设置中可调整连接数、同时下载数、总限速、默认目录和一次性定时启动。
  「队列」按钮停止队列时会暂停活跃任务；再次开启只调度等待队列任务。
  手动暂停的任务需要选择后点击「继续」。
  定时启动要求程序持续运行，不会创建 Windows 后台计划任务。
  退出时保存并暂停活跃任务，下次启动后需手动继续这些任务。

已实现
  HTTP/HTTPS；1–32 个连接；最多同时下载 8 个任务。
  强 ETag 与 Content-Range 校验；中断重试；跨程序重启续传。
  服务端不支持范围请求、未提供强 ETag 时采用单连接完整传输。
  总限速、一次性定时队列、任务持久化、分类筛选、名称/链接搜索。
  速度、进度、剩余时间、每个分段的进度和错误信息。
  TXT 批量链接导入/导出；导入后暂停以便检查；Content-Disposition 文件名。
  完成后 SHA-256；可选预期 SHA-256，不匹配时不发布成品。
  同名文件保留，使用 (1)、(2) 等新名称；下载完成不会自动运行文件。
  已提供浏览器主动发送链接扩展与本地桥接，尚未安装或实际浏览器联调。

数据位置
  任务与设置：%LOCALAPPDATA%\FlowGet\tasks.json
  默认成品：用户的 Downloads\FlowGet，可为每个任务另选目录。
  分段：每个保存目录下的 .flowget-parts\任务ID。
  移除记录会保留成品与分段，不会擅自清理用户数据。
  测试可用 FLOWGET_DATA_DIR 环境变量指定隔离的任务目录。

当前边界
  这是分阶段的早期可运行版本，不是完整 IDM 替代品。
  浏览器自动接管、Cookie/Referer、登录型链接、流媒体发现/HLS/DASH、
  FTP、站点抓取、多队列、导入 IDM 历史、过期链接刷新尚未实现。
  不读取 IDM 密码、下载历史或注册信息，不修改 IDM 文件。
  分段策略是独立固定范围划分，尚未实现动态工作窃取或证明等速。
  目标磁盘需支持硬链接，如 NTFS；FAT/exFAT 不在本版范围内。
  合并过程最多需要约两倍文件大小的可用磁盘空间。
  Windows 默认采用系统 curl（Schannel），域名优先 IPv4。
  Python 后端作为备用，IPv6 专用网络尚未验证。
  任务文件保存完整 URL；含临时签名的 URL 应按个人数据保管。
  没有强 ETag 时会重新完整下载，不承诺所有网站都支持断点续传。
  本版没有导入浏览器登录状态，也不会绕过服务器的权限要求。

项目文件
  app.py           桌面界面。
  engine.py        下载、校验、状态与队列。
  transport.py     系统 curl 的流式适配层。
  native_host.py   Native Messaging 桥接。
  browser-extension/  Edge / Chrome 主动发送链接扩展。
  register-browser.ps1 / unregister-browser.ps1  可审阅的注册与卸载脚本。
  SPECIFICATION.txt 观察证据、独立规格和后续阶段。
  tests/           可重跑的本地 HTTP 边界测试。
  verification/    测试结果与真实 HTTPS 文件的哈希核对结果。
  build.ps1        从源码重新打包。

源码运行
  Python 3.13 + Tk 8.6；仅使用标准库，不需 pip 运行时依赖。
  python app.py

测试
  python -m unittest discover -s tests -p test_engine.py -v
  默认在 Windows 测系统 curl 后端。
  设置 FLOWGET_TEST_BACKEND=urllib 可测试 Python 后端。

与 IDM 的关系
  本程序名为 FlowGet，与 Tonec / Internet Download Manager 无隶属关系。
  没有复制 IDM 的代码、图标、图片、注册机制或私有存储格式。
  本轮是依据可观察界面和公共 HTTP 标准的独立重写；未安排互相隔离的
  两支团队，因此不声称满足有独立人员隔离审计的严格净室证明。
