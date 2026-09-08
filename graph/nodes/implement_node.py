"""
⑤ 功能改寫 Agent（translator-cli + scheduler + Harness 局部驗證）
依 task list 逐一呼叫 translator-cli 實作業務邏輯，並在 module 完工後
呼叫 Harness 局部驗證。見 01_langgraph_architecture.md 六、
02a_harness_architecture.md 十三、07a/07b_translator_cli、
09a_implement_agent_architecture.md。

translator-cli 串接見 07b；Harness 局部驗證串接（DbEnvironment／
GoldenVerifier、熱重載同步屏障、task 失敗根因追蹤、already_failed 修正、
context/context_files 疊加）見本檔案，對應 09a 三～七章。

**Python 服務跑在 Docker 容器內，不直接在 Orchestrator 所在機器上跑**：
見 `python_service/process.py` docstring、`09b_implement_agent_code.md`
十章「已知限制」——`uvicorn --reload` 在 Windows 上經常無法真正完成
重啟（Windows 的 `CTRL_C_EVENT` 送達機制不可靠），已用真實環境重現並
確認容器化（Linux）能穩定繞開這個問題。單例本身（模組層級，比照
`MODEL_SEMAPHORE` 的既有模式）下沉在 `python_service/manager.py`（見
10a 八章），這裡只是啟動／關閉的呼叫端：在整條 graph run 第一次進入
`implement` 時建立，`implement`／`run_tests`／`debug → implement`
重入期間持續共用同一個容器（見 09a 三章「Python 服務只啟動一次」），
由 `main.py` 在 `graph.ainvoke()` 結束後呼叫 `stop_python_service()`
統一關閉。
"""
import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

import httpx
import yaml

from graph.java_source_extraction import (
    JavaSourceExtractionError,
    resolve_java_source,
    resolve_referenced_source,
)
from graph.scheduler import ModuleScheduler
from graph.state import RefactorState, TaskFailure, TaskSpec
from python_service import manager as python_service_manager
from refactor_harness.fixtures.db_env import DbEnvironment
from refactor_harness.verifier.comparator import GoldenVerifier
from translator_cli import client as translator_cli
from translator_cli import ollama_client
from translator_cli.exceptions import TranslatorCliUpstreamDegradedError
from translator_cli.types import FillResult, ReferencedSourceItem

logger = logging.getLogger(__name__)

# 見 07a 七、八章：qwen 實體機器單模型的併發限制收在
# translator_cli/ollama_client.py 自己管理（OLLAMA_MODEL_SEMAPHORE），
# Claude 路徑刻意不受這個限制，這裡不需要外部號誌包住整個 fill_function()
# 呼叫——寫入段的序列化改由 translator_cli/git_ops.py::WRITE_LOCK 負責
# （見 07a 八章「寫入段用細粒度鎖序列化」）。

# 比照 refactor_harness/langgraph_nodes/test_nodes.py 既有讀法，不新增
# 第二份設定來源（見 09a 三章「tables_to_truncate」）。
with open("config/harness.yaml", encoding="utf-8") as f:
    _HARNESS_CONFIG = yaml.safe_load(f)
TABLES = _HARNESS_CONFIG["databases"]["test"]["tables_to_truncate"]

# 見 09a 三章「決定性的同步屏障」／「運行前提」，環境變數可調。
SERVICE_READY_TIMEOUT_SECONDS = float(os.environ.get("SERVICE_READY_TIMEOUT_SECONDS", "120"))
SERVICE_READY_POLL_INTERVAL_SECONDS = float(os.environ.get("SERVICE_READY_POLL_INTERVAL_SECONDS", "2"))

_RELOAD_TOKEN_FILE = "_reload_token.py"
_RELOAD_PROBE_PATH = "/__reload_probe__"

# 見 09a 五章「缺口二：共用 Enum 定義檔」。
_ENUMS_FILE = "app/models/_enums.py"
# 對應 docs/09b_bug_trace.md：_RESULT_FACTORY_INSTANCE_METHOD_NOTICE
# 只用文字描述 ResponseResult／Result 的正確呼叫慣例（如「範例是
# result.msg = msg」），但這個 session 三次真實完整 pipeline 重跑，
# `app/core/exception_handlers.py` 三次都用錯 ResponseResult.error() 的
# 關鍵字參數名稱（如寫成 message= 而不是 msg=）——追查發現這個檔案屬於
# 「全域」模組，不落在 _ENUMS_FILE 那個 if 分支涵蓋的 services／
# repositories 層，context_files 裡從來沒有真的帶上 common_service.py
# 本身，⑤ 只看得到文字提示、看不到真實原始碼可以核對確切的參數名稱，
# 只能用常見英文詞猜（"message" 比 "msg" 更像自然語言，但這個專案的
# 既有慣例是 "msg"）。跟 _RESULT_FACTORY_INSTANCE_METHOD_NOTICE 一樣
# 無條件套用，不看 target_file 落在哪一層——任何層級的 task 都可能
# 呼叫 ResponseResult／Result，提示文字既然無條件疊加，真實原始碼也該
# 無條件一起帶，不能只顧著讓 ⑤「看得到規則」卻看不到「規則描述的對象
# 長什麼樣子」。
_COMMON_SERVICE_FILE = "app/services/common_service.py"
# 見 09a 五章「缺口一：ORM relationship() 缺失」固定提示文字，逐字沿用設計文件內容。
_RELATIONSHIP_GAP_NOTICE = (
    "本專案的 SQLAlchemy model（app/models/{module}.py）只有外鍵純量欄位，"
    "沒有 relationship() 物件導覽屬性（見 08a_scaffold_agent_architecture.md "
    "八章）。禁止用「.關聯屬性」的方式取得關聯物件（例如 order.user 這種寫法"
    "一定會在執行期拋出 AttributeError）；需要關聯資料時，改用額外的 "
    "repository 查詢，或在這個函式內手動用外鍵欄位值另外查詢、自行組裝回傳"
    "結構。"
)
# 見 docs/09b_bug_trace.md「反覆出現的翻譯模式錯誤」：Java 端
# ResponseResult<T>/Result<T> 的 ok()／error()／success() 這些方法在
# Java 原始碼裡是 static 工廠方法（可以直接 ResponseResult.ok(x) 呼叫），
# 但④骨架生成階段（05a 四章「多載方法的處理」既有機制）並不區分 Java
# 的 static／instance 修飾詞，一律渲染成帶 self 的一般 instance method
# （見 app/services/common_service.py::ResponseResult／Result 的實際
# 產出）——直接照 Java 原始碼的寫法呼叫 ResponseResult.ok_2(data) 在
# Python 端會是 TypeError（缺 self），必須先實例化再呼叫，例如
# ResponseResult().ok_2(data)。已重複出現三次獨立案例（exam_service.py
# ::create_random() 呼叫 Result.ok(rs)；file_router.py::voice()／
# image() 呼叫 ResponseResult.ok_2(...)／error_4(...)），跟 relationship
# 缺口不同的是：任何層級（routers／services／repositories）的 task 都
# 可能建構這類回應物件，不像 relationship 只在 services／repositories
# 導覽關聯資料時才會踩到，因此下面 _augment_task_io() 對所有 task 一律
# 疊加，不像 _RELATIONSHIP_GAP_NOTICE 只在 services／repositories 層才加。
_SPECIFICATION_PATTERN_NOTICE = (
    "Java 端若用到 Spring Data JPA 的 Specification<T> 動態查詢 pattern"
    "（方法簽名長得像 withX(...) -> Specification<Entity>，方法本體用 "
    "(root, query, cb) -> Predicate 這種 lambda），Python／SQLAlchemy 端"
    "的對應慣例是：直接回傳一個篩選條件（SQLAlchemy 布林運算式，如 "
    "SomeEntity.field == value），不需要套用這個條件時回傳 None——不要"
    "回傳一個 lambda／closure，也不要呼叫一個不存在的 Specification(...) "
    "建構子（那個型別在 Python 端從未被定義）。呼叫端（組合多個這種"
    "方法回傳值的地方）要蒐集所有不是 None 的條件，一次呼叫 "
    "query.filter(*conditions) 套用——不要把每個回傳值當函式逐一呼叫"
    "（Java Specification.and() 鏈式組合的寫法在 Python 端沒有對應機制，"
    "直接呼叫一個布林運算式會拋 TypeError，見 docs/refactor_bug_trace.md "
    "#14 真實案例）。"
)
_RESULT_FACTORY_INSTANCE_METHOD_NOTICE = (
    "ResponseResult／Result 這類回應包裝類別（app/services/common_service.py）"
    "的 ok()／error()／success() 等方法在 Python 端是 instance method（帶 self"
    "，不是 Java 原始碼裡的 static 工廠方法），呼叫前必須先建立實例，例如 "
    "ResponseResult().ok_2(data)、Result().success(data)——不要直接寫成 "
    "ResponseResult.ok_2(data) 或 Result.success(data)，那樣在 Python 會因為"
    "缺少 self 引數而拋出 TypeError。"
    "如果這個 task 正是在實作 ResponseResult／Result 自己的 ok_2()／error_2()"
    "／success()／failure() 這些方法本體，絕對不要在方法內部呼叫"
    "「ResponseResult().ok_2(...)」或「self.error_2(...)」這種同名／同族方法"
    "呼叫自己（會造成無窮遞迴，永遠不會返回，任何呼叫端都會撞"
    "RecursionError）——正確做法是直接建立一個新實例、把欄位值設好後回傳，"
    "例如 result = ResponseResult(); result.code = code; result.msg = msg; "
    "result.data = data; return result（見 docs/09b_bug_trace.md #53 真實案例）。"
)
# 對應 docs/refactor_bug_trace.md #39：真實案例 `exam_router.py::search()`／
# `save_answer()` 的成功分支手寫 `ResponseResultXxx(code=200, msg="ok", ...)`
# ／`msg="success"`，繞過 `ResponseResult().ok_2(data)` 工廠方法——查真實
# Java 原始碼確認兩者都只是呼叫 `ResponseResult.ok(data)`（或
# `ResponseResult.ok(rs)`），從未自訂訊息文字，`msg` 用的是 `ok()` 內建的
# 預設值「操作成功」；`data` 都翻對了，只有 `msg` 是⑤自己編出來的英文
# 字面字串，跟其他已確認翻對的函式（`get_all()`／`get_single_exam()` 等
# 都是先取 `ok_2(...)` 的結果、再拆 `code`／`msg`／`data` 出來）明顯不
# 一致。跟 `_RESULT_FACTORY_INSTANCE_METHOD_NOTICE` 不同角度：那則只講
# 「呼叫前要先建實例」，沒講「不能繞過工廠方法自己硬寫 code／msg」。
_RESULT_FACTORY_NOT_BYPASSED_NOTICE = (
    "成功分支組回應物件時，`code`／`msg` 一律要透過 "
    "ResponseResult().ok_2(data)（沒有 data 時用 .ok()）取得，再把它的 "
    "code／msg／data 拆出來組進外層回應物件——例如 "
    "rr = ResponseResult().ok_2(data); return Xxx(code=rr.code, msg=rr.msg, "
    "data=rr.data)。禁止手寫 Xxx(code=200, msg=\"ok\") 這種字面字串繞過"
    "工廠方法：即使 Java 原始碼的成功回傳沒有自訂訊息文字（例如只是 "
    "return ResponseResult.ok(data);），也不代表 msg 可以隨便編一個英文"
    "單字——Java 端 ok() 這個方法本身有預設訊息文字，必須讓 Python 對等"
    "的 ok_2() 工廠方法產生同一份預設值，不要自己猜一個占位字串取代"
    "（見 docs/refactor_bug_trace.md #39 真實案例：`msg` 被翻成 \"ok\"／"
    "\"success\" 這種占位英文字，跟真實回應的「操作成功」對不上）。"
)
# 對應 docs/refactor_bug_trace.md #18：retranslate（#9）機制本身正確運作，
# 但真實案例證實它可能「修好一個問題、留下另一個」——⑦ 診斷出
# `exam_router.py::get_all()` 的兩個問題（import 路徑錯、呼叫 find_all()
# 沒傳 db）並要求重翻，⑤ 修對了這兩點，卻在重寫整個函式本體時把
# `ExamkindRepository.find_all(db)` 寫成直接對類別名稱呼叫（沒有先
# `ExamkindRepository()` 建立實例），觸發 `AttributeError: type object
# 'ExamkindRepository' has no attribute 'find_all'`。根因是 Java
# `@Autowired private ExamkindRepository examkindRepository;` 這個欄位
# 在 Spring 啟動時就已經是一個現成的實例，`examkindRepository.findAll()`
# 是對「已存在的實例」呼叫方法；Python 端沒有對應的 DI 容器，必須自己
# `ExamkindRepository()` 建立實例才能呼叫——但這條規則之前只在
# `_RESULT_FACTORY_INSTANCE_METHOD_NOTICE` 裡限定給 ResponseResult／
# Result 兩個特定 class，沒有推廣成「任何 repository／service 類別都要
# 先建實例」的通用規則，⑤ 對其他類別因此沒有提示可循，只能憑直覺翻譯
# Java 的欄位呼叫語法，這次剛好把它誤譯成看起來很像的 static 呼叫。
_REPOSITORY_SERVICE_INSTANTIATION_NOTICE = (
    "Java 端透過 @Autowired 注入的欄位（如 private ExamRepository "
    "examRepository;）在 Spring 啟動時就已經是一個現成的實例，"
    "examRepository.findXxx(...) 這種寫法是對「已存在的實例」呼叫方法，"
    "不是呼叫 static 方法。翻譯成 Python 時沒有對應的 DI 容器自動建立"
    "實例，呼叫任何 repository／service 類別（不限於 ResponseResult／"
    "Result）的方法之前，都必須自己先建立一個實例，例如 "
    "repo = ExamRepository(); repo.find_all(db)——不要直接寫成 "
    "ExamRepository.find_all(db) 這種把類別名稱當呼叫對象的寫法，那樣在"
    "Python 會因為缺少 self 引數而拋出 AttributeError 或 TypeError（見 "
    "docs/refactor_bug_trace.md #18 真實案例）。"
)
# 對應 docs/refactor_bug_trace.md #44：真實案例 `exam_router.py::get_all()`／
# `get_all_exam_kind()`，Java 原始碼裡 `ExamController` 同時宣告了
# `@Autowired private ExamRepository examRepository;` 跟 `@Autowired
# private ExamkindRepository examkindRepository;` 兩個命名相似的欄位，
# 這兩個函式自己的原始碼本體都明確呼叫 `examkindRepository.findAll()`，
# ⑤ 卻翻成 `ExamRepository()`，資料表完全查錯。用 `llmlog` 核對過真實
# prompt，這兩個函式自己的 Java 方法本體（包含正確答案）就直接寫在
# prompt 裡，不是 context 缺口——是模型在同一個 class 有多個相似命名
# repository／service 欄位時，選錯了要呼叫哪一個。使用者提議：既然
# 「有多個相似選項」是這個錯誤模式的共同特徵，明確提示模型看到相似
# 命名時要回頭多核對幾遍，藉此降低（不保證消除）這種選錯的機率。
_SIMILAR_REPOSITORY_DISAMBIGUATION_NOTICE = (
    "如果這個函式對應的 Java class 裡宣告了多個命名相似的 "
    "repository／service 欄位（例如同時有 @Autowired private "
    "ExamRepository examRepository; 跟 @Autowired private "
    "ExamkindRepository examkindRepository; 這種字首相同、只差幾個字的"
    "欄位），呼叫前不要憑名稱相似度或對其他函式的印象去猜要用哪一個—— "
    "回頭把這個函式自己的 Java 原始碼本體逐字讀一遍，找出它實際呼叫的"
    "是哪一個欄位名稱（例如 examkindRepository.findAll()），再對照到"
    "對應的 Python repository／service class，確保兩者是同一個，"
    "找到最相符的那個再翻譯，不要選一個「看起來合理」但其實對應到另一個"
    "相似欄位的class（見 docs/refactor_bug_trace.md #44 真實案例："
    "get_all()／get_all_exam_kind() 的 Java 原始碼明明寫的是 "
    "examkindRepository.findAll()，卻被翻成 ExamRepository()，查到完全"
    "不對的資料表）。"
)
# 見 docs/09b_bug_trace.md 新增條目：Java 端 FileController 的 voice／image
# 上傳下載方法寫死 Windows 磁碟機代號絕對路徑 Paths.get("C:/voice")／
# Paths.get("C:/images")，⑤ 忠實翻譯成 Python 字面字串後，在 Linux 容器
# 內 "C:" 不是磁碟機代號、會被當成一般相對路徑目錄名稱，容器啟動時的
# bind-mount 專案根目錄底下因此真的生出一個叫 "C:" 的資料夾，讓 git
# 工作目錄變髒，拖垮同一輪後續所有 ⑤／⑦ 寫回動作（07a 設計的「乾淨工作
# 樹」前置檢查正確攔下，但攔下的時機已經太晚）。只限定在這一個目標檔案
# 才疊加——這是這個 Java 專案這兩個 method 特有的寫死路徑，不是普遍規則
# （見決策：只修目標專案本身，不在 global_infra.py 新增通用掃描機制）。
# **根因訂正（docs/refactor_bug_trace.md #30）**：這則提示原本要⑤改用
# from app.core.config import VOICE_UPLOAD_DIR 這種 config 常數 import，
# 但整條 pipeline 從沒有任何步驟真的在 app/core/config.py 建立這兩個
# 常數——⑤只是忠實照這則提示的指示寫，import 在執行期直接 ImportError，
# 命中全域例外處理器回傳 500（真實案例：voice／voice_2／image 三個端點
# 全部因此壞掉）。改成直接內嵌容器內的絕對路徑字面字串，不再依賴任何
# config 常數：python_service/process.py 啟動容器時固定把整個生成專案
# bind-mount 到容器內的 /srv（-v {python_project_path}:/srv），"/srv" 底下
# 的子目錄本來就會落在 host 端同一個掛載目錄裡，不需要另外宣告 volume、
# 也不需要 config.py 配合建立常數；已核對 Java 端 voice／image 上傳下載
# 全程不寫資料庫（examkindRepository.voice／picture 欄位是完全不相干的
# 考題音檔檔名，跟這個端點無關），改路徑格式不影響其他任何程式碼。
# 對應 docs/refactor_bug_trace.md #22：真實重跑卡了 30 分鐘，追查到測試 DB
# 裡有一條 idle in transaction 連線從未關閉，擋住下一輪測試的 TRUNCATE。
# 根因：③設計階段逐方法問 LLM「這個方法需不需要 db: Session」
# （needs_db_session_decision），`CandidateController.getCandidate()`／
# `createRandom()` 這兩個 router 方法被判定「不需要」，但它們呼叫的
# `ExamService.getCandidateByCard()`／`createRandom()` 卻被正確判定「需要」
# ——這個不一致（LLM 只看單一方法的 Java 本體，沒有把「呼叫鏈下游需要
# db」的資訊往上游傳）讓這兩個 router 函式的簽名沒有 `db: Session =
# Depends(get_db)`，⑤ 填空時發現本體邏輯需要 db 卻拿不到，只能自己想
# 辦法生一個——真實案例證實兩種瞎猜寫法都出現過：`db = next(get_db())`
# （直接手動驅動 `get_db()` 這個 generator 取第一個 yield 值，但生成器
# 本身不會被呼叫端保留、也不會被 FastAPI 的 DI 機制驅動到 `finally`
# 那一步，`db.close()` 永遠不會執行，連線洩漏，用真實本機重現：打一次
# `/api/candidate/search` 就會在 `pg_stat_activity` 留下一條永遠不會
# 消失的 idle in transaction 連線）；另一個案例（`general_router.py`）
# 已經自己寫對了 `try/finally: db.close()` 這個安全形狀，只是猜錯了
# import 路徑（`from app.db.session import SessionLocal`，這個模組根本
# 不存在，正確路徑是 `app.core.database`）——兩種案例都指向同一個缺口：
# ⑤ 完全沒有被告知「這個專案要怎麼安全地手動取得一個 db session」。
_MANUAL_DB_SESSION_NOTICE = (
    "如果這個函式的簽名沒有 db: Session 參數（不是透過 Depends(get_db) 取得），"
    "但本體邏輯需要呼叫一個要求 db: Session 的 repository／service 方法，"
    "禁止用 db = next(get_db()) 這種方式手動取得 session——get_db() 是設計"
    "給 FastAPI 的 Depends() 機制驅動的 generator，直接手動呼叫 next() 只會"
    "拿到裡面 yield 出來的 Session 物件本身，generator 永遠不會被推進到"
    "它自己的 finally 區塊，db.close() 永遠不會執行，每次呼叫都會洩漏一條"
    "資料庫連線（見 docs/refactor_bug_trace.md #22 真實案例：這樣寫過一次"
    "就會在資料庫留下一條永遠不會消失的 idle in transaction 連線）。"
    "正確做法：from app.core.database import SessionLocal，然後"
    "db = SessionLocal()，把需要用到 db 的邏輯包在 try/finally（finally 裡"
    "呼叫 db.close()）或 with SessionLocal() as db: 區塊內，確保這個手動"
    "建立的 session 一定會被正確關閉——SessionLocal 只能從 "
    "app.core.database import，不要猜成 app.db.session 或其他路徑。"
)
# 對應 docs/refactor_bug_trace.md #24：真實案例 `exam_router.py::
# get_single_exam()`／`get_all()` 把 `ExamkindRepository.find_by_kind()`／
# `find_all()` 回傳的 ORM `ExamkindEntity` 實例轉成同名的 Pydantic
# `ExamkindEntity` schema 物件時，讀取來源欄位也用了 schema 的 camelCase
# 命名（`e.typeNumber`），觸發 `AttributeError`（ORM 實例實際屬性是
# snake_case 的 `type_number`）。用 `llmlog` 逐字核對這次真實 prompt 確認
# 精確根因：reference_targets 只給了 `ExamkindRepository.find_by_kind`
# 這個方法本身的內容（`def find_by_kind(...) -> list[ExamkindEntity]:
# return db.query(ExamkindEntity)...`），從未展示過 `ExamkindEntity`
# **模型類別自己**的欄位宣告（`app/models/exam.py` 那份，snake_case）；
# 反而 `app/schemas/exam.py` 的同名 Pydantic 版本（camelCase）作為
# schema 段落整份都在 context 裡——模型只看得到 camelCase 這一種命名，
# 只能照抄。同一批任務裡 `search()` 剛好沒踩到，是因為它額外呼叫了
# `ExamSpecification.with_X()`（#14 的既有 Specification pattern），
# 那幾個方法的本體剛好把 `ExamEntity.class_name`／`ExamEntity.
# random_id` 這些真正的 snake_case 屬性名稱以參考內容的形式意外帶出來
# 給模型看到——`get_single_exam()`／`get_all()` 沒有呼叫任何
# Specification 方法，沒有這個側面管道，只能瞎猜。這不是隨機的翻譯品質
# 問題，是可重現的 context 資訊落差：只要「repository 方法回傳 ORM
# entity、呼叫端沒有其他管道看到這個 entity 的真實欄位名稱」，就會重演。
_ENTITY_ATTRIBUTE_NAMING_NOTICE = (
    "SQLAlchemy ORM entity（app/models/{module}.py 定義）的實例屬性名稱"
    "一律是 Python 慣例的 snake_case（例如 type_number、class_name、"
    "random_id），不要假設它跟同名的 Pydantic schema（app/schemas/"
    "{module}.py）用一樣的命名慣例——即使兩者剛好是同一個類別名稱（如都"
    "叫 ExamkindEntity），schema 那份為了對齊 API 回應格式常常用 "
    "camelCase（如 typeNumber、className、randomId），是完全不同的兩組"
    "命名，不能混用。從 repository 方法拿到的 ORM entity 物件，讀取欄位"
    "一律用 snake_case（例如 entity.type_number），只有在建構要回傳的"
    "schema 物件時才用 schema 自己宣告的欄位名稱（例如 "
    "SomeSchema(typeNumber=entity.type_number)）——如果不確定這個 entity"
    "實際有哪些 snake_case 欄位，禁止照抄 schema 的欄位名稱去猜，那樣猜"
    "錯會在執行期直接拋出 AttributeError（見 docs/refactor_bug_trace.md "
    "#24 真實案例）。"
)
_FILE_ROUTER_FILE = "app/routers/file_router.py"
_HARDCODED_UPLOAD_PATH_NOTICE = (
    "Java 原始碼的 voice／image 上傳下載方法把儲存目錄寫死成 Windows 磁碟機"
    "代號絕對路徑（Paths.get(\"C:/voice\")、Paths.get(\"C:/images\")）——"
    "不要在 Python 端逐字翻譯成 \"C:/voice\"、\"C:/images\" 這種字面字串，"
    "也不要 import 任何 app.core.config 常數來取代它（例如 "
    "VOICE_UPLOAD_DIR／IMAGE_UPLOAD_DIR）——這個專案的 config.py 目前沒有"
    "、也不會有這兩個常數，那樣寫會在執行期直接 ImportError（見 "
    "docs/refactor_bug_trace.md #30 真實案例）。正確做法：把目錄直接改寫"
    "成這個容器內的絕對路徑字面字串，voice 對應的方法一律用 "
    "\"/srv/uploads/voice\"，image 對應的方法一律用 \"/srv/uploads/images\""
    "（例如 pathlib.Path(\"/srv/uploads/voice\")）——這個專案跑在 Docker "
    "容器裡，整個生成專案本身就 bind-mount 在容器內的 /srv，這個子目錄會"
    "自然落在同一個掛載點下，不需要額外的 volume 或環境變數，也不需要"
    "任何 import。"
)
# 對應 docs/refactor_bug_trace.md #46：真實案例 `file_router.py::
# voice_2()`／`image()`（GET 版本）全部 10 處錯誤分支都寫成 `return
# Response(content=str(err.msg), status_code=XXX)`——只回傳 err.msg 這
# 個純文字字串，丟掉 err.code／err.data，也沒有 JSON 編碼。真實 Java
# （`ResponseEntity.status(...).body(ResponseResult.error(...))`）靠
# Spring 對 POJO body 自動做 Jackson JSON 序列化；這兩個函式的成功分支
# 要回傳二進位檔案內容＋自訂 Content-Type，本來就必須用裸 Response（不能
# 用 Pydantic schema），⑤大概因此把「這個函式回傳型別是 Response」誤
# 推廣成「連錯誤分支也該用裸 Response」，沒意識到錯誤分支仍要維持全
# 專案一致的 JSON 回應慣例。**只在 `task["return_type"] == "Response"`
# 時才疊加**（不是無條件、也不是綁死 file_router.py 這個檔案路徑）——
# `design_agent/type_mapping.py::resolve_api_boundary_signature()` 對
# 原始 Java 簽名是 `ResponseEntity<...>`（依情境動態回傳不同 status／
# body）的函式，已經在③階段機械把 `return_type` 覆寫成 `"Response"`
# （見 #30），這是判斷「這個函式的回傳型別是不是需要自己動態組裝」的
# 真正因果訊號，比檔案路徑這種巧合相關的替代訊號精準；全 Java 專案只有
# 這兩個函式命中，疊加範圍精準、不會稀釋其他 77 個函式的 prompt。
_RAW_RESPONSE_ERROR_JSON_NOTICE = (
    "這個函式的回傳型別是 FastAPI／Starlette 的裸 Response（不是這個"
    "專案自訂的 ResponseResultXxx schema），代表對應的原始 Java 簽名是 "
    "ResponseEntity<...>，依情境動態回傳不同 status／body——只有真正要"
    "回傳二進位內容或自訂 Content-Type 的分支（例如回傳檔案內容本身）"
    "才能用裸 Response，任何錯誤分支仍然要維持跟其他端點一致的 JSON "
    "回應格式：用 `from fastapi.responses import JSONResponse`，"
    "`return JSONResponse(content={\"code\": err.code, \"msg\": err.msg, "
    "\"data\": err.data}, status_code=XXX)`，不能只回傳 "
    "`Response(content=str(err.msg), status_code=XXX)` 這種純文字（見 "
    "docs/refactor_bug_trace.md #46 真實案例：voice／image 的 GET 端點"
    "全部錯誤分支都這樣寫，回應 body 不是合法 JSON，測試連 "
    "json.loads() 都失敗）。"
)
_GLOBAL_EXCEPTION_HANDLER_FILE = "app/core/exception_handlers.py"
# 對應 docs/refactor_bug_trace.md #34：真實案例 `GradingController.
# testException()` 故意 `throw new RuntimeException(...)`、無 try/catch，
# golden fixture 錄到的真實回應是 HTTP 200、body
# {"code":503,"msg":"伺服器內部錯誤","data":null}。根因是 Java 端
# `GlobalExceptionHandler.java::handleAll(Exception e)` 這個
# `@ExceptionHandler(Exception.class)` 方法直接回傳一般物件（不是
# ResponseEntity），沒有 @ResponseStatus，Spring 預設用 200——任何未
# 捕捉例外，HTTP 一律 200，真正錯誤碼放在 JSON body 的 code 欄位裡。
# ⑤ 把回應內容（code／msg／data）翻對了，但直覺把「例外處理」跟「HTTP
# 500」畫上等號，寫死 status_code=500，沒照著這個 runtime 慣例走。只限定
# 這一個目標檔案才疊加——這是這個 Java 專案全域例外處理器特有的行為，
# 不是「所有例外處理都該回 200」這種通用規則。
_GLOBAL_EXCEPTION_HANDLER_STATUS_NOTICE = (
    "這個檔案翻譯的是 Java 端 GlobalExceptionHandler 裡 "
    "@ExceptionHandler(Exception.class) 那個未知錯誤處理方法（handleAll）"
    "——這個方法在 Java 端直接回傳一般物件（不是 ResponseEntity），沒有 "
    "@ResponseStatus 標註，Spring 對這種寫法預設的 HTTP 狀態碼是 200，"
    "不是 500（見 docs/refactor_bug_trace.md #34 真實案例：golden fixture "
    "錄到的真實回應就是 HTTP 200，body 裡才放真正的錯誤碼）。翻譯這個函式"
    "時，JSONResponse 的 status_code 必須寫 200，不要因為這是「處理例外」"
    "就直覺寫成 500——真正的錯誤碼放在回應 body 的 code 欄位（例如 503），"
    "不是 HTTP 狀態碼本身。"
)
# 對應 docs/refactor_bug_trace.md #26：真實案例
# `SchoolRepository.find_by_grade_and_local()` 組出 `conditions =
# [SchoolEntity.grade == grade, SchoolEntity.local == local]` 後，用
# `list(filter(None, conditions))` 想篩掉「不適用」的條件，結果篩選
# 完全沒生效——真實用 SQLAlchemy 物件直接測試確認：
# `bool(SchoolEntity.grade == "國小")` 回傳 `False`（SQLAlchemy 的
# `BinaryExpression` 在 `bool()` 底下本來就恆為 `False`，這是它的既有
# 行為，不是這次執行的偶發結果），`filter(None, conditions)` 依賴
# Python 的一般真假值判斷，把兩個條件都當成「假」濾掉，`filter(*[])`
# 等於沒有任何 WHERE 條件，這個方法因此永遠回傳整張表，`grade`／`local`
# 參數形同虛設——真實測試打 `grade=國小` 卻在結果裡混進國中／高中，
# 100% 可重現，不是隨機翻譯品質問題。跟 #14 的 `_SPECIFICATION_PATTERN_
# NOTICE` 是相關但不同的情境：#14 只講「呼叫 Specification 方法組合出
# 的條件」該怎麼收集，沒有明講「不能用 filter(None, ...)」這條規則，
# 而且 `find_by_grade_and_local()` 根本不是走 Specification pattern
# （repository 方法直接內聯組條件），#14 的提示範圍接不到這裡——任何
# 動態組 SQLAlchemy 篩選條件的地方都可能踩到同一個陷阱，不限於
# Specification pattern，因此獨立成一則提示。
_SQLALCHEMY_CONDITION_FILTERING_NOTICE = (
    "組合多個「可能不適用」的 SQLAlchemy 篩選條件時（例如某個查詢參數是"
    "選填、沒填就不要套用那個條件），篩掉不適用條件一律用 "
    "[c for c in conditions if c is not None]（明確用 is not None 身分"
    "比對），絕對不要用 filter(None, conditions) 或 if cond: 這種一般"
    "真假值判斷來篩選——SQLAlchemy 的欄位比較運算式（如 "
    "SomeEntity.field == value 這種 BinaryExpression）在 bool() 底下"
    "恆為 False，不管這個條件實際上有沒有意義，用真假值判斷會把所有"
    "條件都誤判成「不適用」而整批濾掉，變成完全沒有 WHERE 條件、回傳"
    "整張表（見 docs/refactor_bug_trace.md #26 真實案例：因為這樣寫，"
    "查詢明明帶了篩選參數，結果卻回傳所有資料，篩選完全沒生效）。"
)
# 對應 docs/refactor_bug_trace.md #32／#36：#32 原始案例是 `exam_router.py::
# get_all()`／`get_single_exam()` 手動逐欄位把 ORM `ExamkindEntity` 轉成
# 同名 Pydantic schema 物件時，同時漏掉一模一樣的 4 個欄位（created／
# updated／long_question／text）；查真實 Java 原始碼發現這兩個方法其實是
# `rs.setKinds(examkindRepository.findByKind(...))`——直接把 entity（或
# entity list）整個塞進回應 DTO，全程沒有任何 setXxx() 逐欄位賦值，Java
# 靠 Jackson 自動序列化 entity 的每一個欄位，所以 Python 手動建構時「必須
# 填滿每個欄位」的原始 #32 修法是對的。**但這則提示最初的措辭沒有限定
# 適用範圍，#36 用真實重跑證實它會在另一種情境造成回歸**：
# `grading_router.py::analyze()` 對應的 Java 是 `new QuestionAnalysis()`
# 後明確逐一呼叫 `qa.setTypeNumber(...)`／`qa.setQuestion(...)`／
# `qa.setA(...)` 等特定欄位的 setter——`picture`／`answer`／`score`／
# `kind` 這 4 個欄位雖然也宣告在 schema 上，Java 卻從未 setXxx() 過，故意
# 留 null。套用 #32 原本「必須填滿每個欄位」的指示後，⑤ 改用
# `getattr(question, "picture", None)` 這種寫法把這 4 個欄位硬塞進去，
# 把 Java 故意留 null 的欄位翻成有值，四個欄位全部從真實回應的 null 變成
# 有值，四個都測試失敗——這個 task 的第一次翻譯（attempt 0）就直接帶著
# 這個錯誤，不是重翻或退化。兩種情境的分野在於**Java 原始碼本身怎麼組出
# 這個回應物件**，不是猜 schema 或猜 entity，提示內容因此分成兩支明確
# 對應這兩種情境。
_ENTITY_TO_SCHEMA_FIELD_COMPLETENESS_NOTICE = (
    "手動建構一個 Pydantic schema／DTO 物件時，該填哪些欄位一律以這次"
    "提供的 Java 原始碼實際怎麼組出這個回應物件為準，不要憑印象或只看"
    "schema／entity 有哪些欄位去猜，分兩種情況："
    "(1) 如果 Java 原始碼是把一個 entity 物件（或 entity list）整個直接"
    "塞進回應 DTO 欄位（例如 rs.setKinds(examkindRepository.findByKind"
    "(...))），全程沒有任何 setXxx() 逐欄位賦值——這種情況 Java 靠 "
    "Jackson 自動序列化 entity 的每一個欄位，Python 手動建構同名 schema"
    "物件時必須包含 schema 宣告的每一個欄位，逐一核對，不能只挑看起來"
    "重要的欄位帶過（見 docs/refactor_bug_trace.md #32 真實案例："
    "get_all()／get_single_exam() 屬於這種情況，遺漏 created／updated／"
    "long_question／text 4 個欄位）。"
    "(2) 如果 Java 原始碼是 new 一個新物件後明確逐一呼叫 "
    "qa.setXxx(value) 賦值——這種情況只能填 Java 原始碼真的呼叫過 "
    "setter 的那些欄位，schema 宣告但 Java 從未 setXxx() 過的欄位一律"
    "留給 Pydantic 預設值（通常是 None），不要因為 schema 剛好有這個"
    "欄位、或 entity 剛好有同名屬性可以撈，就用 "
    "getattr(entity, \"field\", None) 這種寫法自作主張硬填進去——那樣"
    "會把 Java 故意留 null 的欄位翻成有值（見 "
    "docs/refactor_bug_trace.md #36 真實案例：analyze() 的 Java 明確只"
    "呼叫了 typeNumber／question／voice／a～d 等特定欄位的 setter，"
    "picture／answer／score／kind 這 4 個欄位 Java 從未設定過，卻被翻成"
    "getattr(question, \"picture\", None) 硬塞進去，四個欄位全部從應該"
    "的 null 變成有值，四個都測試失敗）。"
)


# 對應 docs/refactor_bug_trace.md #43：真實案例 `exam_router.py::
# save_answer()`，⑤ 把 `is_null_or_empty(rq.randomId)` 這個呼叫寫在迴圈
# 判斷式裡，對應的 `from app.utils.validation_util import is_null_or_empty`
# 卻寫在同一個 if 區塊內部、判斷式呼叫**之後**——Python 執行到判斷式當下
# 這個名稱根本還沒被 import，第一次呼叫就 NameError。用 `llmlog` 逐字核對
# 這次真實 prompt，確認系統／使用者提示完全沒有任何一句話規範「函式內
# import 陳述式的位置」，是真正的提示缺口，不是隨機翻譯雜訊。跟 #38 討論
# 過改成「檔案開頭統一 import」（比照 Java 慣例）：`fill_function()` 是
# AST 定位單一函式節點做替換，⑤ 本來就沒有能力改到檔案開頭的 import
# 區塊（見 `apply_file_fix()` docstring），而且同一個檔案的多個函式是
# 各自獨立的 task／LLM 呼叫，各自 local import 互不干涉才是這個架構
# 刻意的設計；改成檔案層級 import 反而會讓 #45 那種同名不同來源的 class
# 從「執行期能被抓到的型別錯誤」惡化成「模組層級靜默互相覆蓋」，範圍
# 只收斂在「函式內 import 必須放在使用之前」這一點。
_IMPORT_BEFORE_USE_NOTICE = (
    "函式本體內任何 import 陳述式，一律要放在這個函式最前面（在任何會"
    "用到那個名稱的程式碼之前），不可以穿插寫在條件判斷式、迴圈或其他"
    "邏輯的中間——即使那段邏輯剛好是第一次用到這個名稱的地方，import "
    "也必須寫在它前面，不是緊接在它後面或同一個區塊裡。Python 執行到"
    "某一行時，用到的名稱必須已經被 import 過，不然會直接拋出 "
    "NameError（見 docs/refactor_bug_trace.md #43 真實案例：`if "
    "is_null_or_empty(rq.randomId) or ...:` 判斷式裡呼叫了 "
    "is_null_or_empty，但 `from app.utils.validation_util import "
    "is_null_or_empty` 寫在同一個 if 區塊內部、判斷式呼叫之後，每次呼叫"
    "都在判斷式那一行就直接 NameError）。"
)


def _augment_task_io(task: TaskSpec) -> tuple[str, list[str]]:
    """對應 09a 五章「疊加規則」：只對 services／repositories 層疊加
    relationship／enum 固定提示，routers 層原樣返回。不修改 task 本身
    （[P] 的權威輸出），只回傳疊加後的 context／context_files 給呼叫端
    傳給 fill_function()。

    ⑦ Debug Agent 上一輪針對這個 task 給的修正不經過這裡——10a 八章
    「⑦ 直接產生修正後程式碼」之後，`pending_fixed_bodies` 裡的內容是
    完整程式碼，直接傳給 `fill_function()` 的 `fixed_body` 參數取代整個
    函式本體，不是疊加進 context 給 ⑤ 本地模型參考（見 `_run_one_task()`）。
    """
    target_file = task["target_files"][0]
    if target_file.startswith("app/repositories/") or target_file.startswith("app/services/"):
        context_files = list(task["target_files"])
        if _ENUMS_FILE not in context_files:
            context_files.append(_ENUMS_FILE)
        existing = task.get("context", "")
        context = f"{existing}\n\n{_RELATIONSHIP_GAP_NOTICE}" if existing else _RELATIONSHIP_GAP_NOTICE
    else:
        context_files = task["target_files"]
        context = task.get("context", "")

    # 見 _RESULT_FACTORY_INSTANCE_METHOD_NOTICE：跟上面的 relationship
    # 提示不同，任何層級的 task 都可能建構 ResponseResult／Result 回應，
    # 無條件疊加，不看 target_file 落在哪一層。
    context = f"{context}\n\n{_RESULT_FACTORY_INSTANCE_METHOD_NOTICE}" if context else _RESULT_FACTORY_INSTANCE_METHOD_NOTICE

    # 見 _RESULT_FACTORY_NOT_BYPASSED_NOTICE：跟上面同一種理由，任何
    # 層級的 task 都可能組成功回應，無條件疊加，不看 target_file 落在
    # 哪一層。
    context = (
        f"{context}\n\n{_RESULT_FACTORY_NOT_BYPASSED_NOTICE}"
        if context
        else _RESULT_FACTORY_NOT_BYPASSED_NOTICE
    )

    # 見 _SPECIFICATION_PATTERN_NOTICE：跟 _RESULT_FACTORY_INSTANCE_METHOD_
    # NOTICE 同一種理由，Specification<T> 的定義方（repository 層）跟
    # 呼叫組合方（router／service 層）都可能是任何 task，無條件疊加。
    context = f"{context}\n\n{_SPECIFICATION_PATTERN_NOTICE}" if context else _SPECIFICATION_PATTERN_NOTICE

    # 見 _COMMON_SERVICE_FILE：跟上面的提示一樣無條件疊加，不看
    # target_file 落在哪一層——任何 task 都可能呼叫 ResponseResult／
    # Result，只給文字規則描述參數慣例不夠，⑤ 需要看到真實原始碼才能
    # 核對確切的參數名稱，不能只能用猜的。這一層 services／repositories
    # 已經把自己複製過 context_files（見上面的 if 分支），所以這裡才能
    # 安全地直接 append，不會動到 task 本身的 target_files。
    if target_file != _COMMON_SERVICE_FILE and _COMMON_SERVICE_FILE not in context_files:
        context_files = list(context_files)
        context_files.append(_COMMON_SERVICE_FILE)

    # 見 _HARDCODED_UPLOAD_PATH_NOTICE：只限定 file_router.py 才疊加。
    if target_file == _FILE_ROUTER_FILE:
        context = f"{context}\n\n{_HARDCODED_UPLOAD_PATH_NOTICE}"

    # 見 _GLOBAL_EXCEPTION_HANDLER_STATUS_NOTICE：只限定這一個目標檔案
    # 才疊加，理由同上面 _HARDCODED_UPLOAD_PATH_NOTICE。
    if target_file == _GLOBAL_EXCEPTION_HANDLER_FILE:
        context = f"{context}\n\n{_GLOBAL_EXCEPTION_HANDLER_STATUS_NOTICE}"

    # 見 _RAW_RESPONSE_ERROR_JSON_NOTICE：只在這個函式的 return_type 真的
    # 是 "Response" 時才疊加——這是③機械算好的因果訊號（見該常數上方
    # 說明），不是看 target_file 這種巧合相關的替代訊號。留 log 記錄
    # 命中的 task，方便之後真實重跑時直接查這個提示有沒有被套用到預期
    # 的 task 上（見 docs/refactor_bug_trace.md #46）。
    if task.get("return_type") == "Response":
        logger.info(
            "task %s（%s）的 return_type 是 Response，疊加 "
            "_RAW_RESPONSE_ERROR_JSON_NOTICE（見 docs/refactor_bug_trace.md #46）",
            task["id"], target_file,
        )
        context = f"{context}\n\n{_RAW_RESPONSE_ERROR_JSON_NOTICE}"

    # 見 _MANUAL_DB_SESSION_NOTICE：跟上面幾個提示同一種理由，無條件疊加、
    # 不看 target_file 落在哪一層——③「這個方法需不需要 db」的判斷是逐
    # 方法各自問 LLM，任何層級都可能被判定「不需要」卻在本體邏輯裡呼叫到
    # 需要 db 的下游（見 #22 真實案例：router 層踩到，但同一個不一致的
    # 判斷機制不保證只在 router 層發生）。
    context = f"{context}\n\n{_MANUAL_DB_SESSION_NOTICE}" if context else _MANUAL_DB_SESSION_NOTICE

    # 見 _REPOSITORY_SERVICE_INSTANTIATION_NOTICE：跟上面幾個提示同一種
    # 理由，無條件疊加——任何層級的 task 都可能呼叫 repository／service
    # 類別的方法，不限於 #18 真實案例踩到的 routers 層。
    context = (
        f"{context}\n\n{_REPOSITORY_SERVICE_INSTANTIATION_NOTICE}"
        if context
        else _REPOSITORY_SERVICE_INSTANTIATION_NOTICE
    )

    # 見 _SIMILAR_REPOSITORY_DISAMBIGUATION_NOTICE：跟上面幾個提示同一種
    # 理由，無條件疊加——任何層級的 task 都可能呼叫到宣告了多個相似命名
    # repository／service 欄位的 Java class，不限於 #44 真實案例踩到的
    # routers 層。
    context = (
        f"{context}\n\n{_SIMILAR_REPOSITORY_DISAMBIGUATION_NOTICE}"
        if context
        else _SIMILAR_REPOSITORY_DISAMBIGUATION_NOTICE
    )

    # 見 _ENTITY_ATTRIBUTE_NAMING_NOTICE：跟上面幾個提示同一種理由，無
    # 條件疊加——任何層級的 task 都可能把 repository 回傳的 ORM entity
    # 轉成 schema 物件，不限於 #24 真實案例踩到的 routers 層。
    context = (
        f"{context}\n\n{_ENTITY_ATTRIBUTE_NAMING_NOTICE}" if context else _ENTITY_ATTRIBUTE_NAMING_NOTICE
    )

    # 見 _SQLALCHEMY_CONDITION_FILTERING_NOTICE：跟上面幾個提示同一種
    # 理由，無條件疊加——任何層級的 task 都可能組合選填的 SQLAlchemy
    # 篩選條件，不限於 #26 真實案例踩到的 repositories 層。
    context = (
        f"{context}\n\n{_SQLALCHEMY_CONDITION_FILTERING_NOTICE}"
        if context
        else _SQLALCHEMY_CONDITION_FILTERING_NOTICE
    )

    # 見 _ENTITY_TO_SCHEMA_FIELD_COMPLETENESS_NOTICE：跟上面幾個提示同一種
    # 理由，無條件疊加——任何層級的 task 都可能把 repository 回傳的 ORM
    # entity 手動轉成 schema 物件，不限於 #32 真實案例踩到的 routers 層。
    context = (
        f"{context}\n\n{_ENTITY_TO_SCHEMA_FIELD_COMPLETENESS_NOTICE}"
        if context
        else _ENTITY_TO_SCHEMA_FIELD_COMPLETENESS_NOTICE
    )

    # 見 _IMPORT_BEFORE_USE_NOTICE：跟上面幾個提示同一種理由，無條件疊加
    # ——任何層級的 task 只要本體邏輯裡用到延遲 import 的名稱，都可能把
    # import 語句放錯位置，不限於 #43 真實案例踩到的 routers 層。
    context = (
        f"{context}\n\n{_IMPORT_BEFORE_USE_NOTICE}" if context else _IMPORT_BEFORE_USE_NOTICE
    )

    return context, context_files


def _target_of(task: TaskSpec) -> tuple[str, str | None, str]:
    """task 的「目標三元組」，見 09a 六章「提前排除」。"""
    return task["target_files"][0], task.get("class_name"), task["function_name"]


def _is_scaffold_skipped(task: TaskSpec, skipped_interfaces: list[dict]) -> bool:
    """精確 tuple 相等比對，不做模糊匹配，見 09a 六章「三元組比對的正確性」。"""
    target = _target_of(task)
    return any(
        (s["file_path"], s["class_name"], s["function_name"]) == target
        for s in skipped_interfaces
    )


def _make_task_failure(task: TaskSpec, reason: str, error: str) -> TaskFailure:
    file_path, class_name, function_name = _target_of(task)
    return TaskFailure(
        task_id=task["id"],
        module=task["module"],
        file_path=file_path,
        class_name=class_name,
        function_name=function_name,
        reason=reason,
        error=error,
    )


def _RETRANSLATE_HINT(diagnosis: str) -> str:
    return (
        "⑦ Debug Agent 診斷這個函式先前的版本（或從沒成功生成過）有以下問題，"
        f"這次翻譯請務必依照下方 java_source 的真實邏輯正確實作，避免重蹈同樣的問題：{diagnosis}"
    )


async def _run_one_task(
    task: TaskSpec, python_project_path: str, java_project_path: str, run_id: str,
    pending_fixed_bodies: dict[str, str],
    pending_retranslate_tasks: dict[str, str] | None = None,
) -> FillResult:
    """`pending_fixed_bodies` 裡有這個 task 的 id 時，代表 ⑦ Debug Agent
    已經給出修正後的完整函式本體（見 10a 八章「⑦ 直接產生修正後程式
    碼」），直接傳給 `fill_function()` 的 `fixed_body`，完全跳過模型
    呼叫——不需要再解析 `java_source`／`referenced_source`、也不需要再組
    context／context_files 給模型參考。

    `pending_retranslate_tasks` 裡有這個 task 的 id 時（對應
    `docs/refactor_bug_trace.md` #9），代表 ⑦ 判定這個 task 的函式本體
    需要重新翻譯，而不是由 ⑦ 自己寫 `fixed_body`——這個分支**不**跳過
    模型呼叫，走跟⑤首輪翻譯完全相同的路徑（下方照常解析真實
    `java_source`／`referenced_source`、真的呼叫 `fill_function()`），
    唯一差異是把 ⑦ 的 `diagnosis` 疊加進 `context`，讓模型知道上一輪
    （或從沒成功翻譯過）錯在哪。跟 `pending_fixed_bodies` 互斥（見
    `debug_agent/analysis.py` 的建構邏輯，同一個 task_id 不會同時出現在
    兩份清單裡），這裡不特別檢查衝突。

    `java_source`／`referenced_source` 在這裡（呼叫 `fill_function()`
    之前）解析——`task["java_method_id"]`／`task["reference_targets"]`
    是 06a 六章
    機械算出的座標，把座標解析成真正的原始碼文字是⑤（這裡）的職責，不是
    translator-cli 的職責（見 07a 五章「為什麼是 java_source／
    referenced_source」、`graph/java_source_extraction.py` 模組
    docstring）。`java_source` 解析失敗（理論上不該發生，見
    `JavaSourceExtractionError` docstring）直接讓這個 task 失敗，不呼叫
    `fill_function()`——沒有 `java_source`，模型沒有翻譯依據；
    `referenced_source` 單一項目解析失敗由 `resolve_referenced_source()`
    自行記警告並跳過，不影響這個 task 本身能不能被翻譯（見該函式
    docstring）。
    """
    fixed_body = pending_fixed_bodies.get(task["id"])
    context, context_files = _augment_task_io(task)
    retranslate_diagnosis = (pending_retranslate_tasks or {}).get(task["id"])
    if retranslate_diagnosis:
        hint = _RETRANSLATE_HINT(retranslate_diagnosis)
        context = f"{context}\n\n{hint}" if context else hint
    referenced_functions = [
        (ref["file_path"], ref["class_name"], ref["function_name"])
        for ref in task.get("referenced_functions", [])
    ]

    java_source = ""
    referenced_source: list[ReferencedSourceItem] = []
    if fixed_body is None:
        try:
            java_source = resolve_java_source(java_project_path, task["java_method_id"])
        except JavaSourceExtractionError as exc:
            return FillResult(success=False, error=f"java_source 解析失敗：{exc}", diff="")
        referenced_source = resolve_referenced_source(
            java_project_path, python_project_path, task.get("reference_targets", [])
        )

    return await translator_cli.fill_function(
        python_project_path=python_project_path,
        task_id=task["id"],
        target_file=task["target_files"][0],
        class_name=task.get("class_name"),
        function_name=task["function_name"],
        translator_backend=task["translator_backend"],
        java_source=java_source,
        referenced_source=referenced_source,
        description=task["description"],
        context=context,
        context_files=context_files,
        run_id=run_id,
        referenced_functions=referenced_functions,
        fixed_body=fixed_body,
    )


async def _partial_verify(module: str, db: DbEnvironment, verifier: GoldenVerifier) -> dict:
    """對應 09a 三章「批次執行」／「必須包 asyncio.to_thread()」：db／
    verifier 都是同步 API，`run()` 是 async def（LangGraph 對 async def
    node 不會自動丟執行緒池，見 09a 三章「為什麼不需要額外的鎖機制」上方
    段落），這裡自己包一層，避免佔住主事件迴圈。
    """
    await asyncio.to_thread(db.apply_seed, "fixtures/seed.sql", tables_to_truncate=TABLES)
    return await asyncio.to_thread(
        verifier.verify_module,
        collection_path="postman/collection_readonly.json",
        module_filter=module,
    )


def _module_has_failed_task(scheduler: ModuleScheduler, module: str) -> bool:
    module_task_ids = {t["id"] for t in scheduler.tasks_by_module.get(module, [])}
    return bool(module_task_ids & scheduler.task_failed)


async def _get_reload_probe_id(python_base_url: str) -> None:
    """對應 09a 三章「運行前提」的一次性初始探測：容忍連線暫時被拒絕，
    重試到任何一次成功回應為止，只確認服務目前有在跑，不比對回應內容。
    只在整條 graph run 真正第一次進入 implement 時才由 run() 觸發。
    """
    deadline = time.monotonic() + SERVICE_READY_TIMEOUT_SECONDS
    async with httpx.AsyncClient() as client:
        while True:
            try:
                resp = await client.get(f"{python_base_url}{_RELOAD_PROBE_PATH}", timeout=5.0)
                if resp.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Python 服務初始探測逾時（{SERVICE_READY_TIMEOUT_SECONDS}s）："
                    f"{python_base_url}{_RELOAD_PROBE_PATH} 未回應，見 09a 三章「運行前提」——"
                    "服務可能沒有以 uvicorn _reload_probe_wrapper:wrapper_app --reload 啟動"
                )
            await asyncio.sleep(SERVICE_READY_POLL_INTERVAL_SECONDS)


async def _wait_for_service_reload(python_project_path: str, python_base_url: str) -> bool:
    """對應 09a 三章「決定性的同步屏障」：產生新 token、寫入
    _reload_token.py、輪詢 /__reload_probe__ 直到回應等於這個 token。
    逾時回傳 False（不拋例外，見「逾時不該讓整條 pipeline 崩潰」），由
    呼叫端就地把這一輪要驗證的 module 全部標記失敗。
    """
    token = str(uuid.uuid4())
    token_path = Path(python_project_path) / _RELOAD_TOKEN_FILE
    await asyncio.to_thread(token_path.write_text, f'TOKEN = "{token}"\n', encoding="utf-8")

    deadline = time.monotonic() + SERVICE_READY_TIMEOUT_SECONDS
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{python_base_url}{_RELOAD_PROBE_PATH}", timeout=5.0)
                if resp.status_code == 200 and resp.text == token:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(SERVICE_READY_POLL_INTERVAL_SECONDS)
    return False


# _python_service 單例本身下沉到 python_service/manager.py（見 10a 八章
# 「診斷資料改走 State，不是 debug_agent/ 直接 import implement_node」）
# ——這裡只是委派呼叫，不再自己持有這個變數；`refactor_harness/
# langgraph_nodes/test_nodes.py`（⑥）健康檢查失敗時也需要讀取同一個
# 容器的診斷資料，因此不能讓這個單例只屬於這個 node 檔案。
async def _ensure_python_service_started(state: RefactorState) -> None:
    # 對應 docs/09b_bug_trace.md #46：把③輸出的 config_env_vars（見
    # graph/state.py PythonStructure.config_env_vars）跟 java_project_path
    # 一併傳給 manager，讓它在真正啟動容器前解析出 Java 端 application-
    # {profile}.properties 的實際值，當額外 -e 環境變數注入。沒有任何
    # @Value 欄位的專案 state["python_structure"] 不會有這個 key，
    # .get(...) 回傳 None，manager.ensure_started() 據此不做任何事，
    # 行為等同這個機制不存在。
    await python_service_manager.ensure_started(
        state["python_project_path"],
        state["python_base_url"],
        java_project_path=state["java_project_path"],
        config_env_vars=state["python_structure"].get("config_env_vars"),
    )


async def stop_python_service() -> None:
    """main.py 在整條 graph run 結束（不論成功或失敗）時呼叫一次，關閉
    並移除容器——Docker 容器不會隨 Python process 結束自動清理。
    """
    await python_service_manager.stop()


def should_run_tests_or_give_up(state: RefactorState) -> str:
    """對應 01 五章「scaffold 失敗時的收尾路徑」：④ 骨架生成失敗時，
    run_tests（⑥）打的是一個從未被正確產出程式碼的 Python 服務——02a
    五章「Newman 共用執行器」明訂服務沒起來時 newman 執行器會直接拋出
    例外，不是回傳一筆失敗的比對結果。若放任 `implement` 之後無條件接
    `run_tests`，這個例外會讓整條 `graph.ainvoke()` 崩潰，根本走不到
    `should_debug_or_done()` 這個既有的 conditional edge。

    這裡在 `implement → run_tests` 之間插入這道判斷：`implement` 只有
    單一前驅（`scaffold → implement` 才是 fan-in，這裡改的不是那個合流
    點，不影響 01 五章已經驗證過的平行分支語意）。`debug → implement`
    的重試迴圈對 `generate_scaffold()` 的失敗無能為力（07a 十三章：
    working tree 不乾淨等錯誤「需要人工介入核對，不是可以自動化解的
    暫時性錯誤」），因此直接跳 `give_up`，不浪費重試次數在注定不會
    改變結果的迴圈上。
    """
    return "give_up" if state.get("scaffold_done") is False else "run_tests"


async def run(state: RefactorState) -> RefactorState:
    # ④ 骨架生成若失敗，這個 pipeline run 底下不會有任何一個
    # target_file 真的存在，逐一呼叫 fill_function() 只會各自用
    # FileNotFoundError 快速失敗（見 07a 六章步驟 1「scaffold/task 不
    # 一致」），不需要真的跑一輪排程器才知道結果。提前在這裡短路：
    #
    # 全部標記成 failed_modules，不是 blocked_modules——blocked_modules
    # 不消耗 retry_count（見 debug_node.py），若這裡誤用 blocked 會讓
    # debug → implement 永遠原地打轉。標記成 failed_modules 才能讓既有的
    # should_debug_or_done() 正確判斷，但實際路由改由上面的
    # should_run_tests_or_give_up() 接手，不會真的再跑一次 run_tests。
    if state.get("scaffold_done") is False:
        return {
            **state,
            "completed_tasks": [],
            "failed_tasks": [],
            "task_failures": [],
            "partial_reports": [],
            "blocked_modules": [],
            "failed_modules": [m["module"] for m in state["module_list"]],
            "blocked_reasons": {},
        }

    # 恢復前一輪執行（debug 迴圈重入）的進度
    latest_module_status = {}
    for r in state.get("partial_reports", []):
        latest_module_status[r["module"]] = r["report"]["status"]
    already_verified_modules = {
        module for module, status in latest_module_status.items() if status == "pass"
    }
    # 見 docs/09b_bug_trace.md #41：module 一旦所有 task 都已完成，重建
    # scheduler 後不會再被排進 touched_modules，"failed" 狀態必須跟
    # "verified" 一樣明確恢復，否則會預設落回 "pending" 被誤判成
    # blocked_modules，讓 debug ↔ implement 陷入不會終止的迴圈。
    already_failed_modules = {
        module for module, status in latest_module_status.items() if status == "fail"
    }

    # 提前排除：scaffold 缺口在排程階段就永久跳過，不等 fill_function()
    # 失敗才發現，見 09a 六章「提前排除」。
    skipped_interfaces = state.get("skipped_interfaces", [])
    existing_failure_ids = {f["task_id"] for f in state.get("task_failures", [])}
    scaffold_gap_task_ids: set[str] = set()
    new_scaffold_gap_failures: list[TaskFailure] = []
    for task in state["task_list"]:
        if _is_scaffold_skipped(task, skipped_interfaces):
            scaffold_gap_task_ids.add(task["id"])
            if task["id"] not in existing_failure_ids:
                new_scaffold_gap_failures.append(
                    _make_task_failure(
                        task, "scaffold_skipped", "④ 骨架階段未渲染此函式，見 skipped_interfaces"
                    )
                )

    # 見 docs/09b_bug_trace.md：09a 七章原本只把 scaffold_gap_task_ids
    # 當成 already_failed，理由是「這次還沒重試過的翻譯品質失敗」不該被
    # 永久排除——這條規則在 10a 落地、⑦ Debug Agent 開始接手除錯迴圈之後
    # 沒有跟著修正，導致排程器每一輪重建時，仍然把所有 fill_failed 的
    # task 當成全新、可以再排給 ⑤ 本地模型的 task，即使 ⑦ 這一輪完全沒
    # 分析到它。真實環境重跑證實：這正是同一批 task 連續 3 輪、9 次
    # attempt 全部透過 Ollama 重試、卻從未真正交給 ⑦ 的機制性原因——
    # 不是 prompt 沒要求 ⑦ 出手，是排程器本身就會把它排回 ⑤，跟 ⑦ 有沒有
    # 出手無關。
    #
    # 10a 的既有決策（⑤ 的本地模型一旦進入除錯迴圈就完全退出）優先於
    # 09a 這條更早、範圍更窄的規則：一個 task 只要曾經在 task_failures
    # 留下一筆 reason=="fill_failed"，就永久排除在排程器的「可排給 ⑤」
    # 池之外，唯一能讓它重新被排到的路徑是 force_reschedule()（見下方，
    # 只由 ⑦ 產生的 pending_fixed_bodies 觸發）——跟 scaffold_gap 同一種
    # 「機械永久排除、只由更高權限的機制解除」的處理方式，不需要另外的
    # 資料結構，直接併入同一個 already_failed 集合。
    fill_failed_task_ids = {
        f["task_id"] for f in state.get("task_failures", []) if f["reason"] == "fill_failed"
    }

    scheduler = ModuleScheduler(
        state["module_list"],
        state["task_list"],
        already_completed=set(state.get("completed_tasks", [])),
        already_failed=scaffold_gap_task_ids | fill_failed_task_ids,
        already_verified_modules=already_verified_modules,
        already_failed_modules=already_failed_modules,
    )

    # 見 10a 八章「新增：ModuleScheduler.force_reschedule()」：
    # pending_fixed_bodies／pending_retranslate_tasks 可能指向一個
    # module_status=="verified"／"failed" 的 module（⑤ 局部驗證誤判為
    # verified、但 ⑥ 全量驗證抓到真正問題；或模組已經被判定 failed，⑦
    # 判定這個 task 可以修好）——這兩種狀態的模組，get_ready_tasks() 都
    # 不會再排到它們的 task。這裡把對應 module 打回 pending，並把被指名
    # 的 task_id 從 task_done／task_failed 移除，讓它真的能被重新排程
    # ——不論是直接套用 ⑦ 給的 fixed_body，還是走 pending_retranslate_
    # tasks 重新呼叫模型翻譯（見 docs/refactor_bug_trace.md #9）。這一段
    # 對兩份清單一視同仁，不區分是哪一種 origin 產生的修正。
    pending_fixed_bodies = state.get("pending_fixed_bodies", {})
    pending_retranslate_tasks = state.get("pending_retranslate_tasks", {})
    tasks_to_reopen_by_module: dict[str, set[str]] = {}
    for task_id in {*pending_fixed_bodies, *pending_retranslate_tasks}:
        task = next((t for t in state["task_list"] if t["id"] == task_id), None)
        if task is not None:
            tasks_to_reopen_by_module.setdefault(task["module"], set()).add(task_id)
    for module, task_ids in tasks_to_reopen_by_module.items():
        scheduler.force_reschedule(module, task_ids)

    partial_reports: list[dict] = []

    # 見 09a 七章「100% 由 scaffold_gap_task_ids 覆蓋的 module」：這種
    # module 永遠不會出現在 touched_modules 裡，必須在這裡提前標記，
    # 否則停在 pending、被歸進 blocked_modules，在沒有其他 failed_modules
    # 時會讓 debug ↔ implement 無限循環（retry_count 永遠不遞增）。
    for module, tasks in scheduler.tasks_by_module.items():
        if tasks and all(t["id"] in scaffold_gap_task_ids for t in tasks):
            scheduler.mark_module_verified(module, passed=False)
            partial_reports.append({
                "module": module,
                "round": state.get("retry_count", 0),
                "report": {
                    "status": "fail",
                    "reason": "module_entirely_scaffold_skipped",
                    "regression": False,
                },
            })

    db = DbEnvironment(test_dsn=state["test_dsn"])
    verifier = GoldenVerifier(python_base_url=state["python_base_url"], golden_dir="fixtures/golden")

    # 見 09a 三章「debug 重入時必須跳過」：只在整條 graph run 真正第一次
    # 進入 implement 時才啟動 Python 服務容器，debug → implement 重入時
    # 完全跳過（_ensure_python_service_started() 本身也是冪等的，容器
    # 已在跑就直接 return，這裡額外用 is_first_entry 判斷純粹對齊 09a
    # 三章原文「只在真正第一次進入時才做這次前置確認」的語意）。
    is_first_entry = not state.get("completed_tasks") and not state.get("task_failures")
    if is_first_entry:
        await _ensure_python_service_started(state)
        # 見 docs/refactor_bug_trace.md #7：只在這次 run 真的有 qwen／
        # repository task 時才需要確認 ollama 連得到——沒有 qwen task 的
        # run 不該因為這個預檢而被誤擋。跟上面的 Python 服務探測同一種
        # 「操作前提沒滿足，不吞、直接往上拋」原則，在真正燒掉任何一次
        # 模型呼叫預算之前就先發現連不上，不用等 UPSTREAM_DEGRADED_
        # THRESHOLD 次真正失敗才觸發。
        if any(t.get("translator_backend") == "qwen" for t in state["task_list"]):
            await ollama_client.check_ollama_reachable()

    completed, failed = [], []
    task_failures: list[TaskFailure] = list(new_scaffold_gap_failures)

    # 見 10a 八章「⑦ 直接產生程式碼機制的 phase 2」：pending_file_fixes
    # 是檔案層級的修正（如 import 敘述），fill_function() 的 AST 函式
    # 定位機制碰不到，改用 translator_cli.apply_file_fix() 的精確字串
    # 替換。這些修正不對應任何要重新生成的函式本體，不透過排程器（不會
    # 出現在 get_ready_tasks() 裡）；套用完直接顯式等一次服務重啟，不
    # 依賴下面 while 迴圈「有其他 task 在跑」才會觸發的既有
    # reload-wait 時機——若這裡沒有其他 task 可以當「順風車」，file_fix
    # 寫入磁碟後就不會有任何東西觸發 reload-wait，⑥ 下一輪的全量驗證
    # 可能讀到還沒 reload 的舊服務狀態。
    file_fix_applied = False
    for file_fix in state.get("pending_file_fixes", []):
        result = await translator_cli.apply_file_fix(
            python_project_path=state["python_project_path"],
            task_id=file_fix["task_id"],
            target_file=file_fix["target_file"],
            old_snippet=file_fix["old_snippet"],
            new_snippet=file_fix["new_snippet"],
        )
        if result.success:
            file_fix_applied = True
        else:
            task = next((t for t in state["task_list"] if t["id"] == file_fix["task_id"]), None)
            if task is not None:
                task_failures.append(_make_task_failure(task, "file_fix_failed", result.error or ""))
    if file_fix_applied:
        # 逾時不視為這幾個 file_fix 失敗（比照 09a 三章「逾時不該讓整條
        # pipeline 崩潰」的既有精神）——這裡只是盡量給服務重啟的時間，
        # 「是否真的修好」交給 ⑥ 下一輪的全量驗證判斷，不是這裡的職責。
        await _wait_for_service_reload(state["python_project_path"], state["python_base_url"])

    while not scheduler.all_done():
        ready = scheduler.get_ready_tasks()
        if not ready:
            break  # 沒有可執行的 task：全部做完、卡在失敗的上游 module，或還有 needs_reverify 待處理

        # 排程層可以同時把多個就緒 task 丟進 gather（見 07a 七、八章）：
        # qwen task 會在 translator_cli/ollama_client.py 內部
        # 的 OLLAMA_MODEL_SEMAPHORE 排隊序列化，Claude task 可以真正平行
        # 呼叫模型，寫入磁碟那一小段兩者都會在 translator_cli/git_ops.py
        # 的 WRITE_LOCK 底下序列化。pending_fixed_bodies 命中的 task 不
        # 呼叫任何模型，直接套用 ⑦ 給的程式碼；pending_retranslate_tasks
        # 命中的 task 則正常呼叫模型（見 docs/refactor_bug_trace.md #9），
        # 但寫入段兩者都一樣要走 WRITE_LOCK，仍然共用同一個 gather，不需要
        # 另外分流。
        results = await asyncio.gather(
            *(
                _run_one_task(
                    t, state["python_project_path"], state["java_project_path"], state["run_id"],
                    pending_fixed_bodies, pending_retranslate_tasks,
                )
                for t in ready
            )
        )

        touched_modules = set()
        upstream_degraded = False
        upstream_degraded_error = ""
        for task, result in zip(ready, results):
            scheduler.mark_task_done(task, result.success)
            touched_modules.add(task["module"])

            # 心跳 log：對應 docs/09b_bug_trace.md #34——這次真實重跑卡住時，
            # log 完全安靜超過一小時，因為 task 成功時原本什麼都不印（只有
            # 失敗／重試才有 log），沒辦法只靠「log 有沒有新東西」判斷是
            # 正常在跑還是卡死。每個 task 做完（不論成功失敗）都固定印一行，
            # 讓「長時間沒有這行」變成一個對 implement 階段也有效的卡住訊號。
            logger.info(
                "task %s %s（module=%s）", task["id"],
                "成功" if result.success else "失敗", task["module"],
            )

            if result.success:
                completed.append(task["id"])
                # regression 偵測：只傳 target_files[0]（實際寫入目標），
                # 見 graph/scheduler.py module_owned_files 註解。
                for regressed in scheduler.check_upstream_regression(
                    [task["target_files"][0]], skip_module=task["module"]
                ):
                    scheduler.flag_for_reverify(regressed)
            else:
                failed.append(task["id"])
                task_failures.append(_make_task_failure(task, "fill_failed", result.error or ""))
                if result.upstream_degraded:
                    upstream_degraded = True
                    upstream_degraded_error = result.error or ""

        pending_verify = [m for m in touched_modules if scheduler.module_ready_for_verification(m)]
        pending_reverify = [m for m, s in scheduler.module_status.items() if s == "needs_reverify"]

        # 見 09a 三章「要不要驗證某個 module」：這個 module 底下有失敗
        # task → 不呼叫 Newman，直接判定沒過，不需要真的等重啟。
        for module in pending_verify:
            if _module_has_failed_task(scheduler, module):
                scheduler.mark_module_verified(module, passed=False)

        verify_needs_wait = [m for m in pending_verify if not _module_has_failed_task(scheduler, m)]
        reverify_needs_wait = list(pending_reverify)

        # 見 09a 三章「批次執行」：整輪只呼叫一次，不是每個 module 各自呼叫。
        if verify_needs_wait or reverify_needs_wait:
            reload_ok = await _wait_for_service_reload(state["python_project_path"], state["python_base_url"])
            if not reload_ok:
                # 見 09a 三章「逾時不該讓整條 pipeline 崩潰」：就地接住，
                # 不讓例外往外傳，這一輪要驗證的每個 module 都判定沒過。
                for module in verify_needs_wait:
                    partial_reports.append({
                        "module": module,
                        "round": state.get("retry_count", 0),
                        "report": {"status": "fail", "reason": "batch_reload_timeout", "regression": False},
                    })
                    scheduler.mark_module_verified(module, passed=False)
                for module in reverify_needs_wait:
                    partial_reports.append({
                        "module": module,
                        "round": state.get("retry_count", 0),
                        "report": {"status": "fail", "reason": "batch_reload_timeout", "regression": True},
                    })
                    scheduler.mark_module_verified(module, passed=False)
            else:
                for module in verify_needs_wait:
                    report = await _partial_verify(module, db, verifier)
                    report["regression"] = False
                    partial_reports.append({"module": module, "round": state.get("retry_count", 0), "report": report})
                    scheduler.mark_module_verified(module, passed=report["status"] == "pass")
                for module in reverify_needs_wait:
                    report = await _partial_verify(module, db, verifier)
                    report["regression"] = True
                    partial_reports.append({"module": module, "round": state.get("retry_count", 0), "report": report})
                    scheduler.mark_module_verified(module, passed=report["status"] == "pass")

        if upstream_degraded:
            # 見 translator_cli/exceptions.py::TranslatorCliUpstreamDegradedError、
            # docs/09b_bug_trace.md #35、docs/refactor_bug_trace.md #1：連續
            # 多個 task 各自獨立地在傳輸層失敗，懷疑是上游 ollama／nginx
            # 服務本身異常或這台機器打不到它——這是操作前提沒滿足（網路／
            # 服務健康狀態），不是可以透過 debug → implement 重試迴圈修好
            # 的翻譯品質問題。真實案例證實：軟性停止（只停這一輪排程、
            # pipeline 照常往下走）會讓同一個 module 內其餘不受這個問題
            # 影響的 Claude 後端 task（router／service 層）因為
            # `graph/scheduler.py::_backfill_missing_task_deps()` 的同
            # module 自動序列依賴被連坐卡住一整輪，且 debug／run_tests
            # 繼續空轉、白白消耗 retry_count 與 Claude API 呼叫，卻無法
            # 真正解決問題——這一輪已經觸發的驗證（跟 ollama 無關，是打
            # 本地 Python 服務的 harness 驗證）照常做完之後，直接中止整條
            # `graph.ainvoke()`，比照 09a 三章「服務打從一開始就沒被正確
            # 啟動」同一種「操作前提沒滿足，不吞、直接往上拋」的既有原則。
            logger.error(
                "偵測到疑似上游模型服務異常（連續多個 task 在傳輸層失敗），"
                "中止整條 pipeline，請確認 OLLAMA_BASE_URL／網路連線／ollama 服務健康狀態後再重跑"
            )
            raise TranslatorCliUpstreamDegradedError(
                "偵測到連續多次 ollama 呼叫在傳輸層失敗，懷疑是網路連線或 "
                "ollama／nginx 服務本身異常，不是可以透過 debug 重試修好的翻譯"
                "品質問題——已中止整條 pipeline，請確認 OLLAMA_BASE_URL 是否"
                f"正確、這台機器是否連得到該服務後再重跑。原始錯誤：{upstream_degraded_error}"
            )

    # while 迴圈跳出的兩種可能，對 run_tests/debug 的意義完全不同：
    # - blocked：從未被排到（上游從沒驗證過，屬於「程式碼不存在」）
    # - failed：曾經驗證過但沒過（不論是原生失敗還是 regression 造成的失敗，屬於「程式碼寫錯」）
    blocked_modules = [m for m, s in scheduler.module_status.items() if s == "pending"]
    failed_modules = [m for m, s in scheduler.module_status.items() if s == "failed"]
    # 對應 docs/refactor_bug_trace.md #13：blocked／failed 只涵蓋
    # "pending"／"failed" 兩種狀態，一個 module 若卡在 "in_progress"
    # 永遠到不了終態（如 #12 死結修正前的真實案例：部分 task 成功、但
    # router 從沒被排到，`module_ready_for_verification()` 永遠不成立），
    # 兩份清單都不會列到它——`common/run_report.py` 判斷「確認修好」時
    # 若只看「沒有落在 failed／blocked 裡」會把這種情況誤判成修好。這裡
    # 額外明確列出真正 "verified" 的 module，讓「確認修好」有辦法用正面
    # 訊號判斷（該模組真的驗證通過），不是用兩個不完整的負面清單去推論。
    verified_modules = [m for m, s in scheduler.module_status.items() if s == "verified"]

    # 純附加診斷資訊，不改變 ModuleScheduler 任何放行邏輯（見
    # docs/09b_bug_trace.md #29「先查證根因，不假設、不改排程邏輯」）：
    # 對每個 blocked module，列出它 depends_on 裡狀態還不是 "verified"
    # 的直接上游 module 名稱，讓「這個 module 到底被誰卡住」不需要人工
    # 反查 module_list.depends_on 就能直接讀出來。
    blocked_reasons = {
        m: [d for d in scheduler.modules[m]["depends_on"] if scheduler.module_status.get(d) != "verified"]
        for m in blocked_modules
    }

    return {
        **state,
        "completed_tasks": completed,
        "failed_tasks": failed,
        "task_failures": task_failures,
        "partial_reports": partial_reports,
        "blocked_modules": blocked_modules,
        "failed_modules": failed_modules,
        "verified_modules": verified_modules,
        "blocked_reasons": blocked_reasons,
    }
