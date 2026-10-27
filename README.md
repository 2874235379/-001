# FacePass · 日常上班人脸打卡系统

本地优先的人脸考勤系统：**FastAPI + WebSocket + dlib（ResNet 128 维）+ MySQL 5.7 + 原生前端**。
摄像头画面不出本机，人脸特征与打卡记录只写进你自己的 MySQL。

- 实时识别：浏览器 `getUserMedia` → Canvas 压缩（480px / 7fps）→ WebSocket 单帧一问一答
- 活体检测：dlib 68 点关键点计算 EAR，**必须完成一次眨眼**才允许比对，照片 / 屏幕翻拍会停在「请眨眼」
- Apple Face ID 观感：四角括号 + 扫描线 + 毛玻璃、成功时对勾 `stroke-dasharray` 一笔画出并轻微回弹、失败时识别框变红横向抖动
- 业务规则：2 分钟内重复识别不写库；按上下班时间自动判定 `正常 / 迟到 / 早退`
- 环境自检：缺 dlib、缺模型、MySQL 连不上，页面和终端都会给出**可执行的修复建议**，不会静默失败

---

## 1. 项目结构

```
打卡/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI 入口：REST + WebSocket + 静态托管 + 全局异常
│   ├── config.py          # .env / 环境变量 -> Settings（不写死路径与密码）
│   ├── db.py              # MySQL 连接池 + 数据访问（pymysql，utf8mb4）
│   ├── face_service.py    # dlib 封装：检测 / 68 点 / 128 维特征 / EAR / 比对
│   ├── checkin.py         # 活体状态机（纯逻辑，可单测）
│   ├── attendance.py      # 考勤状态判定（纯函数）
│   ├── netinfo.py         # 取本机局域网 IP（启动日志用）
│   └── static/
│       ├── index.html     # 打卡主界面 + 录入弹窗 + 环境诊断抽屉
│       ├── style.css      # 玻璃拟态 / 扫描线 / 对勾 / 抖动 / 明暗主题
│       └── app.js         # 摄像头、WebSocket、UI 状态机、记录轮询
├── certs/                 # 自签证书（gen_cert.py 生成，git 忽略）
├── models/                # dlib 模型放这里（见 models/README.md）
├── scripts/
│   ├── init_db.py         # 一键建库建表（读取 sql/schema.sql）
│   ├── check_env.py       # 环境自检：Python / 依赖 / MySQL / dlib / 模型
│   ├── gen_cert.py        # 生成局域网 HTTPS 自签证书（手机摄像头需要）
│   ├── open_firewall.ps1  # 放行 8010 / 8443（仅本地子网）
│   └── enroll.py          # 命令行录入员工人脸
├── sql/
│   └── schema.sql         # 建库建表 SQL（可直接在小皮面板执行）
├── tests/
│   ├── test_logic.py      # 40 项纯逻辑测试（EAR / 活体 / 考勤 / 比对 / BLOB）
│   ├── test_db.py         # 14 项数据库集成测试（自动清理测试数据）
│   ├── test_ws_flow.py    # 17 项 WebSocket 端到端测试（注入假引擎）
│   └── demo_server.py     # 无 dlib 时的 UI 预览服务（假引擎，仅供看动效）
├── tools/
│   └── cdp_check.mjs      # 零依赖 Chrome CDP 运行时验收脚本
├── .env                   # 本机实际配置（已按当前电脑填好）
├── .env.example           # 配置模板
├── requirements.txt
├── start.bat              # 双击启动
├── open_firewall.bat      # 双击放行防火墙（会弹 UAC）
└── README.md
```

---

## 2. 快速开始

### 2.1 环境要求

| 组件 | 要求 | 说明 |
| --- | --- | --- |
| Python | **3.9+**（本项目在 3.13.12 上实测通过） | dlib 需要预编译 wheel 或本地编译，见 2.3 |
| MySQL | 5.7.x（小皮面板 / phpStudy） | 表结构兼容 5.7，字符集 `utf8mb4` |
| 浏览器 | Chrome / Edge 最新版 | `getUserMedia` 只在 `localhost` 或 https 下可用 |

### 2.2 建库（两种方式任选）

**方式 A：小皮面板（推荐给不熟悉命令行的同学）**

1. 打开小皮面板 →「数据库」→「创建数据库」→ 名称填 `face_attendance`，字符集选 `utf8mb4`。
2. 选中该库 →「SQL 执行」→ 把 `sql/schema.sql` 全文粘贴进去 → 执行。
3. 右侧应出现三张表：`employees`、`face_encodings`、`attendance_records`。

**方式 B：命令行一键建库建表**

```bat
python scripts/init_db.py
```

脚本会自动创建数据库（若不存在）、执行建表语句、最后打印表清单。
需要清空重建时：`python scripts/init_db.py --drop`（**会删除三张表的数据**）。

### 2.3 安装依赖

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install fastapi "uvicorn[standard]" pymysql numpy pillow python-multipart
```

**dlib 单独处理**（PyPI 只提供源码包，没有 Windows wheel，直接 `pip install dlib` 会触发本地编译）：

- **方案 A（省事）**：`conda install -c conda-forge dlib`
- **方案 B（本项目实测）**：下载与 Python 版本匹配的社区预编译 wheel，例如
  `dlib-20.0.99-cp313-cp313-win_amd64.whl`，然后
  `.venv\Scripts\python -m pip install dlib-20.0.99-cp313-cp313-win_amd64.whl`
- **方案 C（源码编译）**：装 CMake + Visual Studio Build Tools（C++ 桌面开发工作负载），再 `pip install dlib`

> wheel 是社区构建、非 PyPI 官方产物，请自行评估来源。没有 dlib 时服务仍能启动，
> 页面会明确提示「未安装 dlib」并给出修复入口，不会白屏或假装识别成功。

### 2.4 下载 dlib 模型

```text
http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2
http://dlib.net/files/dlib_face_recognition_resnet_model_v1.dat.bz2
```

解压后把两个 `.dat` 放进 `models/`，文件名必须是：

- `models/shape_predictor_68_face_landmarks.dat`（约 99.7 MB）
- `models/dlib_face_recognition_resnet_model_v1.dat`（约 22.1 MB）

> Windows 资源管理器不能直接解压 `.bz2`，用 7-Zip / Bandizip / 360 压缩即可。
> 只解压出 `.dat.bz2` 而没解压出 `.dat` 时，自检会提示「检测到 .bz2，请先解压」。
>
> 下载慢的话：dlib.net 官方源最稳但速度一般；GitHub raw 对大文件会间歇性 502；
> 也可以从 PyPI 的 `face_recognition_models` 包里取同名模型文件（它是 dlib 官方模型的打包，约 100MB）。
> 项目放在中文路径下也没问题——dlib 在 Windows 上对非 ASCII 路径敏感，代码会自动把模型缓存到系统临时目录再加载。

### 2.5 配置

复制 `.env.example` 为 `.env`（仓库里已经带了一份按本机填好的 `.env`），重点改这几项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DB_HOST` / `DB_PORT` | `127.0.0.1` / `3306` | 小皮面板 MySQL 地址 |
| `DB_USER` / `DB_PASSWORD` | `root` / `root` | 本机实测密码是 `123456`，已写入 `.env`；换机器请改 |
| `DB_NAME` | `face_attendance` | 与面板里创建的库名一致 |
| `DLIB_SHAPE_PREDICTOR` | `models/shape_predictor_68_face_landmarks.dat` | 相对项目根目录，也可写绝对路径 |
| `DLIB_FACE_RECOGNITION_MODEL` | `models/dlib_face_recognition_resnet_model_v1.dat` | 同上 |
| `FACE_DETECTOR` | `hog` | `hog` CPU 友好；`cnn` 更准但需要额外模型且慢 |
| `MATCH_THRESHOLD` | `0.45` | 欧氏距离小于该值判定同一人，越小越严格 |
| `EAR_THRESHOLD` | `0.25` | 眼睛长宽比低于该值视为闭眼 |
| `EAR_CONSECUTIVE_FRAMES` | `2` | 连续多少帧闭眼确认眨眼 |
| `LIVENESS_TIMEOUT_SECONDS` | `9` | 单次识别等待上限，超时判活体失败 |
| `WORK_START` / `WORK_END` | `09:00` / `18:00` | 上下班时间 |
| `LATE_GRACE_MINUTES` | `0` | 迟到宽限分钟数 |
| `DUPLICATE_WINDOW_SECONDS` | `120` | 重复打卡窗口 |
| `AUTO_PUNCH_TYPE` | `true` | 当天第一次=上班，之后=下班 |
| `APP_HOST` | `0.0.0.0` | `0.0.0.0` 允许局域网访问；只想本机用就改回 `127.0.0.1` |
| `APP_PORT` | `8010` | 本机 8000 常被占用，默认用 8010 |
| `HTTPS_PORT` | `8443` | 手机访问专用端口（需证书） |
| `SSL_CERTFILE` / `SSL_KEYFILE` | `certs/dev-cert.pem` / `certs/dev-key.pem` | `scripts/gen_cert.py` 生成；留空则只跑 HTTP |

### 2.6 自检 + 启动

```bat
python scripts/check_env.py     :: 逐项检查，失败项直接给修复命令
python -m app.main              :: 或双击 start.bat
```

打开 http://127.0.0.1:8010 ，允许摄像头权限。
需要手机 / 局域网访问时：先双击 `open_firewall.bat` 放行防火墙（只放行本地子网，需要点一次 UAC），
再按第 6 节用 `https://<本机IP>:8443` 打开（手机摄像头必须 https）。

### 2.7 录入员工

**方式 A：网页录入（推荐）**

右上角「录入员工」→ 填工号 / 姓名 / 部门 → 让本人单独出现在画面中 →「采集并录入」。
系统会间隔 0.5 秒自动抓 3 帧，每帧要求恰好一张人脸，全部成功才写库；重新录入会覆盖旧样本。

**方式 B：命令行录入（适合批量、或没有浏览器时）**

```bat
python scripts/enroll.py --emp-no E001 --name 张三 --dept 研发部 --image a.jpg b.jpg c.jpg
python scripts/enroll.py --list
```

### 2.8 打卡

正对镜头 → 自然眨一次眼 → 屏幕出现绿色对勾与「打卡成功，欢迎 XXX」。
2 分钟内再次识别会提示「已打过卡」且不写新记录。

---

## 3. 算法与业务规则

### 3.1 识别链路

1. **检测**：dlib HOG 检测器，先把帧缩到 480px 宽再检测（CPU 单帧约 30~60ms）。
2. **关键点**：在原始帧上跑 `shape_predictor_68_face_landmarks`，取 68 点。
3. **活体**：左右眼各 6 点计算 EAR：
   `EAR = (|p2-p6| + |p3-p5|) / (2 × |p1-p4|)`，双眼取平均。
   - 前 3 帧先校准个人睁眼基线（取最大值），因为实测有人睁眼 EAR 只有 0.19；
   - 实际闭眼阈值 = `min(0.25, 基线 × 0.72)`，下限 0.14 —— 默认仍是 0.25，
     但不会把眼型偏小的人一直判成「闭眼」；
   - **连续 2 帧 EAR 低于该阈值** 判定眨眼，随后必须重新睁眼（> 阈值 × 1.1）才提取特征；
   - 活体分数 = `(基线 - 最小EAR) / 基线 / 0.55`，截断到 `[0,1]` 落库。
4. **特征**：`dlib.face_recognition_model_v1` 输出 128 维向量（float64 → 存 float32）。
5. **比对**：与库中所有样本算欧氏距离，同一员工取最小距离；`< 0.45` 判为同一人。

### 3.2 考勤判定

- **打卡类型**：默认 `AUTO_PUNCH_TYPE=true`，当天第一条记录 = `上班`，之后 = `下班`。
  这样中午 13:00 才到公司的人，第一条会被判成「上班 / 迟到」，而不会误判成「早退」。
- **状态**：
  - 上班：`打卡时间 > WORK_START + LATE_GRACE_MINUTES` → `迟到`，否则 `正常`
  - 下班：`打卡时间 < WORK_END` → `早退`，否则 `正常`
- 同一天多次打卡都会留痕（第 3 次仍记 `下班`），`正常/迟到/早退` 只描述当次行为。

### 3.3 防重复

写入前在事务内 `SELECT ... FOR UPDATE` 锁住员工行，再查最近 `DUPLICATE_WINDOW_SECONDS` 内的记录：
命中则返回重复（前端提示「已打过卡（时间）」），不插新行。多台设备并发也不会写出两条。

---

## 4. 数据库设计

| 表 | 作用 | 关键字段 / 索引 |
| --- | --- | --- |
| `employees` | 员工信息 | `uk_emp_no` 唯一工号；`idx_department`、`idx_active` |
| `face_encodings` | 人脸样本 | `encoding` BLOB（512 字节 float32）、`dims`、`sample_index`；`idx_employee_sample`；外键级联删除 |
| `attendance_records` | 打卡记录 | 冗余 `emp_no / employee_name / department`；`idx_employee_date`、`idx_date_time`、`idx_punch_time`、`idx_status` |

三张表均为 InnoDB + `utf8mb4_unicode_ci`，兼容 MySQL 5.7（未使用 JSON 字段、CHECK 约束、窗口函数）。
完整 DDL 见 `sql/schema.sql`。

---

## 5. 接口

### 5.1 REST

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 数据库 + dlib + 模型路径的完整自检 |
| GET | `/api/config` | 前端可见的配置（不含密码） |
| GET | `/api/overview` | 员工数、今日打卡人数、最近 30 条记录 |
| GET | `/api/records?date_str=YYYY-MM-DD` | 指定日期记录 |
| GET | `/api/employees` | 员工列表与样本数 |
| POST | `/api/employees/enroll` | 表单：`emp_no`、`name`、`department`、`files`（1~5 张） |
| WS | `/ws/checkin` | 打卡主通道 |

错误统一返回：

```json
{ "ok": false, "code": "db_error", "message": "数据库 face_attendance 不存在", "hint": "运行 python scripts/init_db.py 自动创建。" }
```

### 5.2 WebSocket 协议

客户端发**二进制 JPEG 帧**（单帧 ≤3MB），每帧必有一条 JSON 回执（前端收到回执才发下一帧，天然背压）。

```jsonc
// 连接后服务端主动下发
{"type":"hello","config":{...},"engine":{"ready":true,...}}

// 过程状态
{"type":"state","state":"scanning","hint":"保持正对摄像头，自然眨一次眼",
 "face":true,"blink":false,"progress":0.32,"ear":0.31,"bbox":[0.24,0.2,0.4,0.46]}

// 成功
{"type":"result","ok":true,"code":"success","message":"打卡成功，欢迎 张三",
 "employee":{"emp_no":"E001","name":"张三","department":"研发部"},
 "punch_type":"上班","status":"正常","time":"09:12:33",
 "liveness_score":0.95,"distance":0.31,"note":"上班时间 09:00，打卡 09:12"}

// 2 分钟内重复
{"type":"result","ok":true,"code":"duplicate","message":"已打过卡（09:12:33）","last":{...}}

// 失败
{"type":"result","ok":false,"code":"unknown","message":"未识别到员工","distance":0.62,"threshold":0.45}
{"type":"result","ok":false,"code":"liveness_failed","message":"请眨眼，活体检测失败"}

// 环境问题
{"type":"error","code":"face_engine_unavailable","message":"未安装 dlib…","hint":"pip install dlib==19.24.6…"}
```

客户端可发送 `{"type":"reset"}` 立即开始新一轮（前端在结果展示 2.6~3.4 秒后自动发送）。

---

## 6. 局域网 / 手机访问

### 6.1 为什么会 ERR_CONNECTION_REFUSED

这是三个互相独立的关卡，任何一关没过都会「拒绝连接」：

1. **监听地址**：uvicorn 默认只绑 `127.0.0.1`，只有本机回环能连进来。
   其他设备的数据包到了网卡，但系统里没有进程在 `0.0.0.0:8010` 上接收，于是拒绝。
2. **Windows 防火墙**：即使绑了 `0.0.0.0`，入站 TCP 8010 没有放行规则也会被**静默丢弃**。
   本机 Wi-Fi 被识别为「公用网络」时更严格（当前就是 Public）。
3. **IP / 网段**：手机不在同一网段、连的是开了 AP 隔离的访客 Wi-Fi、或本机 IP 已经变了。

本项目的对应修复：

- `.env` 里 `APP_HOST=0.0.0.0`，启动日志会直接打印可用的局域网地址；
- `open_firewall.bat` 一键添加入站规则（TCP 8010 / 8443，**仅 LocalSubnet**，需要点一次 UAC）；
- 启动时会打印「本机访问 / 局域网访问 / 手机访问」三行地址，照着用即可。

自查：

```bat
netstat -ano | findstr :8010
:: 应看到 0.0.0.0:8010 LISTENING

powershell -ExecutionPolicy Bypass -File scripts\open_firewall.ps1 -Check
:: 查看两条规则是否存在

open_firewall.bat
:: 双击运行，UAC 点「是」，脚本会自动放行 8010 / 8443
```

### 6.2 手机能打开页面，但摄像头还是不可用？

因为 `getUserMedia` 只在**安全上下文**里可用：`https://` 或 `localhost`。
`http://192.168.2.100:8010` 不是安全上下文，`navigator.mediaDevices` 直接是 `undefined`。

本项目已经内置了 HTTPS 方案：

```bat
python scripts/gen_cert.py          :: 生成带本机局域网 IP 的自签证书
python -m app.main                  :: 同时监听 8010(HTTP) 和 8443(HTTPS)
```

手机打开 `https://192.168.2.100:8443`，证书警告选「高级 → 继续前往」即可调用摄像头。

- 桌面 Chrome 临时测试也可以加启动参数（不需要证书）：
  `chrome.exe --unsafely-treat-insecure-origin-as-secure=http://192.168.2.100:8010 --user-data-dir=D:\tmp\chrome-test`
- 如果手机系统强制要求可信证书：用 mkcert 生成并在手机上安装根证书，或用 Caddy 反代 + 内网域名。
- 自签证书只适合内网开发，不要用于公网。

### 6.3 安全提醒

- 系统**没有登录认证**：局域网内任何人打开页面都能查看记录、录入人脸。
  只在可信 Wi-Fi 下开启，用完把 `APP_HOST` 改回 `127.0.0.1` 或直接关掉服务。
- 防火墙规则已限制 `RemoteAddress LocalSubnet`，但仍然不要把 8010 / 8443 映射到公网。
- 人脸特征属于生物识别信息，局域网共享前请确认同事知情。

---

## 7. 常见问题

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| `pip install dlib` 报 `CMake is not installed` / 编译几十分钟 | PyPI 只有源码包 | 用 conda-forge，或下载匹配 Python 版本的社区 wheel，或装 CMake + VS Build Tools 自行编译 |
| 页面显示「未安装 dlib」 | 当前解释器没装 dlib | 用运行服务的那个 Python 执行 `pip install dlib==19.24.6` |
| 页面显示「缺少 dlib 模型文件」 | `.dat` 没放对 / 只解压了 `.bz2` | 按 `models/README.md` 放好，运行 `python scripts/check_env.py` 看确切路径 |
| `ERROR 1045 Access denied` | 账号密码不对 | 改 `.env` 的 `DB_USER/DB_PASSWORD`；小皮面板可在「数据库」页重置 |
| `ERROR 1049 Unknown database` | 库没建 | 面板里新建 `face_attendance`，或跑 `python scripts/init_db.py` |
| `ERROR 2003 Can't connect` | MySQL 没启动 | 小皮面板点「启动」MySQL，确认 3306 未被占用 |
| 浏览器拿不到摄像头 | 用了 `http://192.168.x.x` 访问 | 改用 `http://127.0.0.1:8010` 或 `localhost`；局域网访问需 https |
| 一直「请自然眨一次眼」 | 光线太暗 / 戴反光眼镜 / 摄像头太远 | 补光、正对镜头；可把 `EAR_THRESHOLD` 调到 0.22~0.23 |
| 总是「未识别到员工」且距离接近阈值 | 录入样本少或光线差异大 | 重新录入 3 张不同角度；或把 `MATCH_THRESHOLD` 调到 0.5（误识风险上升） |
| 中文路径导致模型加载失败 | 个别 dlib 版本对非 ASCII 路径敏感 | 把项目放到纯英文路径；或把 `models/` 复制到 `C:\facepass-models` 并在 `.env` 写绝对路径 |
| 端口被占用 | 8000/8010 已被别的进程使用 | 改 `.env` 的 `APP_PORT`，例如 8020 |
| 局域网 / 手机访问 `ERR_CONNECTION_REFUSED` | 只监听了 127.0.0.1，或防火墙没放行 | `APP_HOST=0.0.0.0` + 运行 `open_firewall.bat`，见第 6 节 |
| 手机能打开页面，但摄像头报错 / 无反应 | http 不是安全上下文 | 改用 `https://<IP>:8443`（`python scripts/gen_cert.py`） |

---

## 8. 安全与边界

- **数据不出本机**：画面只在内存中处理，不落盘、不上传；数据库也只连 `127.0.0.1`。
- **活体强度**：EAR 眨眼检测可以挡住静态照片、大部分手机屏幕翻拍，但**不是金融级 3D 活体**
  （高帧率视频回放、精细 3D 面具仍可能绕过）。不要用于支付、门禁等高安全场景。
- **隐私**：`face_encodings` 存的是不可逆的 128 维特征，不是人脸照片；但仍属于生物识别信息，
  建议限制数据库访问权限、定期备份并加密存储，员工离职时删除对应记录（外键级联）。
- **局域网暴露**：`APP_HOST=0.0.0.0` 时同网段设备都能访问，且没有登录认证；仅在可信网络临时开启。
- **生产部署**：建议只监听 `127.0.0.1`，放在 Nginx / Caddy 后面加 HTTPS 与认证；不要把 8010 / 8443 直接暴露到公网。

---

## 9. 我是怎么验证的

在同一台机器（Windows 11 / MySQL 5.7.26）上实际跑过：

```bat
python tests/test_logic.py     :: 40 项通过
python tests/test_db.py        :: 14 项通过（真实 MySQL，自动清理）
python tests/test_ws_flow.py   :: 17 项通过（WebSocket 全链路，注入假引擎）
python tests/test_face_real.py --image a.jpg b.jpg   :: 13 项通过（真实 dlib）
python scripts/check_env.py    :: 逐项自检
```

真实引擎（dlib 20.0.99 + 官方模型）实测数据：

- 同一张人脸缩放到 480px 并调亮 8% 后，欧氏距离 **0.037**
- 两个不同人的距离 **0.608**（阈值 0.45）
- 静态照片连续送帧：9 秒内始终检测不到眨眼 → `liveness_failed`，数据库 **0 条记录**
- 中文路径：dlib 会报 `Unable to open ...`，代码自动缓存到 `%TEMP%\facepass-models` 后正常加载

前端用本机 Chrome + CDP（`tools/cdp_check.mjs`，虚拟摄像头）验证：
页面无 console 报错、无网络失败，摄像头出流，环境就绪 / 环境异常态 / 诊断抽屉 / 明暗主题 / 移动端布局均正常。

局域网相关（2026-10-25 补充验证）：

- `netstat` 确认 `0.0.0.0:8010` 与 `0.0.0.0:8443` 同时 LISTENING；
- 从本机走局域网 IP 访问 `http://192.168.2.100:8010`、`https://192.168.2.100:8443` 均返回 200；
- Chrome CDP 打开 `https://192.168.2.100:8443`，虚拟摄像头成功出流（1280×720），无 console 报错——
  说明 HTTPS + LAN IP 的安全上下文可以正常调用摄像头；
- Windows 防火墙当前**没有** 8010 / 8443 的入站规则（非管理员无法添加），
  所以其他设备要访问，需要先运行一次 `open_firewall.bat`（UAC 点「是」）。

> 本机是 Python 3.13.12 + 社区 cp313 wheel，模型已放在 `models/`，开箱即用。
> 换机器时按 2.3 安装 dlib、按 2.4 放模型即可；缺任何一项，页面会明确提示而不是假装成功。

---

## 10. 需求对照

| 需求 | 实现位置 |
| --- | --- |
| 前端实时摄像头、5~10fps 截帧 | `app/static/app.js`（7fps、480px、JPEG 0.72、一问一答背压） |
| WebSocket 实时识别 | `app/main.py` `/ws/checkin` |
| EAR 眨眼活体、连续 2 帧、阈值 0.25 | `app/face_service.py` `ear_from_landmarks` + `app/checkin.py` |
| 扫描中括号 / 扫描线 / 毛玻璃 | `app/static/style.css` `.brackets` `.scanline` |
| 成功对勾 `stroke-dasharray` + 回弹 | `style.css` `.check-ring` `.check-mark` `@keyframes check-pop` |
| 失败变红 + 横向抖动 + 文案 | `style.css` `.stage[data-state="error"]` `@keyframes shake` + `app.js renderResult` |
| 2 分钟防重复、不写新记录 | `app/db.py` `record_attendance`（行锁 + 窗口查询） |
| 正常 / 迟到 / 早退 | `app/attendance.py` `decide_punch` |
| 三张表 + 索引 + utf8mb4 + 5.7 兼容 | `sql/schema.sql` |
| 缺 dlib / 缺模型 / 连不上库时明确报错 | `app/config.py`、`app/db.py`、`app/face_service.py`、`scripts/check_env.py`、前端诊断抽屉 |
