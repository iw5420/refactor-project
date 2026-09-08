"""ModuleScheduler.force_reschedule()，對應 docs/10a_debug_agent_architecture.md
八章「新增：ModuleScheduler.force_reschedule()」。"""
from graph.scheduler import ModuleScheduler


def _module(name: str, depends_on: list[str] | None = None) -> dict:
    return {"module": name, "java_files": [], "depends_on": depends_on or [], "methods": [], "summary": ""}


def _task(task_id: str, module: str) -> dict:
    return {
        "id": task_id, "module": module, "description": "d",
        "target_files": [f"app/services/{module}_service.py"], "context": "",
        "depends_on": [], "class_name": None, "function_name": "do_it",
    }


def _phased_task(task_id: str, module: str, phase: int, layer: str, translator_backend: str | None = None) -> dict:
    task = {
        "id": task_id, "module": module, "description": "d",
        "target_files": [f"app/{layer}/{module}_x.py"], "context": "",
        "depends_on": [], "class_name": None, "function_name": "do_it", "phase": phase,
    }
    if translator_backend is not None:
        task["translator_backend"] = translator_backend
    return task


class TestForceReschedule:
    def test_verified_module_task_becomes_ready_again(self):
        module_list = [_module("exam")]
        task_list = [_task("task_1", "exam")]
        scheduler = ModuleScheduler(
            module_list, task_list,
            already_completed={"task_1"},
            already_verified_modules={"exam"},
        )
        assert scheduler.get_ready_tasks() == []  # verified 且 task 已完成，本來不會再排到

        scheduler.force_reschedule("exam", {"task_1"})

        assert scheduler.module_status["exam"] == "pending"
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"task_1"}

    def test_only_named_task_reopened_others_stay_done(self):
        # 同一 module 有兩個 task，pending_fixed_bodies 只指名其中一個
        # ——另一個已完成的 task 不該被誤重新排程。
        module_list = [_module("exam")]
        task_list = [_task("task_1", "exam"), _task("task_2", "exam")]
        scheduler = ModuleScheduler(
            module_list, task_list,
            already_completed={"task_1", "task_2"},
            already_verified_modules={"exam"},
        )

        scheduler.force_reschedule("exam", {"task_1"})

        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"task_1"}
        assert "task_2" in scheduler.task_done


class TestGlobalPhaseTierBarrier:
    """對應 refactor_plan.md 一章「三層全域關卡」：全專案 Phase 1 全部
    完成才放行任何 module 的 service task，service 全部完成才放行任何
    module 的 controller/router task——跨 module 的全域關卡，不是同
    module 內部的排序規則。"""

    def test_service_task_blocked_until_all_phase1_tasks_terminal(self):
        module_list = [_module("exam")]
        task_list = [
            _phased_task("repo_1", "exam", phase=1, layer="repositories"),
            _phased_task("svc_1", "exam", phase=2, layer="services"),
        ]
        scheduler = ModuleScheduler(module_list, task_list)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"repo_1"}  # service 還沒放行，Phase 1 沒完成

        scheduler.mark_task_done(task_list[0], success=True)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"svc_1"}

    def test_service_task_blocked_by_phase1_task_in_a_different_module(self):
        # 全域關卡：exam 自己的 Phase 1 完成了，但只要「全專案」還有其他
        # module 的 Phase 1 task 沒完成，exam 的 service task 也不放行。
        module_list = [_module("exam"), _module("school")]
        task_list = [
            _phased_task("repo_exam", "exam", phase=1, layer="repositories"),
            _phased_task("svc_exam", "exam", phase=2, layer="services"),
            _phased_task("repo_school", "school", phase=1, layer="repositories"),
        ]
        scheduler = ModuleScheduler(
            module_list, task_list, already_completed={"repo_exam"},
        )
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"repo_school"}  # svc_exam 仍被 school 的 Phase 1 卡住

    def test_permanently_failed_phase1_task_counts_as_terminal_not_deadlock(self):
        # scaffold 缺口這類永久失敗的 task 永遠不會進 task_done——關卡必須
        # 用「終態（成功或失敗）」判斷，只等成功會讓關卡永遠卡死。放在不同
        # module，避免同 module 內 _backfill_missing_task_deps() 的序列
        # 依賴（要求前一個 task 必須成功）混進來，那是另一條獨立機制。
        module_list = [_module("exam"), _module("school")]
        task_list = [
            _phased_task("repo_1", "exam", phase=1, layer="repositories"),
            _phased_task("svc_1", "school", phase=2, layer="services"),
        ]
        scheduler = ModuleScheduler(module_list, task_list, already_failed={"repo_1"})
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"svc_1"}

    def test_router_task_blocked_until_all_service_tasks_terminal(self):
        module_list = [_module("exam")]
        task_list = [
            _phased_task("svc_1", "exam", phase=2, layer="services"),
            _phased_task("router_1", "exam", phase=2, layer="routers"),
        ]
        scheduler = ModuleScheduler(module_list, task_list)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"svc_1"}  # Phase 1 已經是空集合（沒有任何 tier 0 task），
        # 但 router 仍要等 service 這一層，不是連帶被放行

        scheduler.mark_task_done(task_list[0], success=True)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"router_1"}

    def test_phase1_tasks_are_never_blocked_by_the_tier_barrier(self):
        # 分散在不同 module，避免同 module 內 _backfill_missing_task_deps()
        # 的序列依賴（另一條獨立機制）混進這條斷言。
        module_list = [_module("exam"), _module("school"), _module("grading"), _module("file")]
        task_list = [
            _phased_task("repo_1", "exam", phase=1, layer="repositories"),
            _phased_task("util_1", "school", phase=1, layer="utils"),
            _phased_task("svc_1", "grading", phase=2, layer="services"),
            _phased_task("router_1", "file", phase=2, layer="routers"),
        ]
        scheduler = ModuleScheduler(module_list, task_list)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"repo_1", "util_1"}

    def test_missing_phase_field_defaults_to_tier_zero_backward_compatible(self):
        # 既有呼叫端（測試 fixture、01 文件 stub）不一定會補 phase 欄位
        # （NotRequired，見 graph/state.py TaskSpec）——缺席時不能讓這批
        # task 被新關卡卡住，預設視同 Phase 1（tier 0，限制最少）。
        module_list = [_module("exam")]
        task_list = [_task("task_1", "exam")]  # 舊版 _task()，沒有 phase 欄位
        scheduler = ModuleScheduler(module_list, task_list)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"task_1"}

    def test_repository_task_not_blocked_by_module_dependency_on_a_pure_service_module(self):
        # 對應 docs/refactor_bug_trace.md #8：真實死結重現。common module
        # 只有 service 層（tier 1）task（如 ResponseResult／Result 工廠
        # 方法），沒有任何 repository（tier 0）內容；exam module 在
        # module 層級依賴 common（這個專案的常見寫法，業務 module 依賴
        # common 取得共用回應型別）。修正前：common 的 tier 1 task 要等
        # 全專案 tier 0 完成才放行，但 exam 的 repository task（tier 0）
        # 又被 module 依賴卡住、要等 common 先驗證通過——兩邊互相等對方，
        # get_ready_tasks() 永遠回傳空清單，是真正的死結，不是理論案例。
        module_list = [
            _module("common"),
            _module("exam", depends_on=["common"]),
        ]
        task_list = [
            _phased_task("svc_common", "common", phase=2, layer="services"),
            _phased_task("repo_exam", "exam", phase=1, layer="repositories"),
        ]
        scheduler = ModuleScheduler(module_list, task_list)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        # exam 的 repository task 不受「exam 依賴 common」這條 module
        # 規則影響，立刻可排；common 的 service task 還在等全域 tier 0
        # 完成（此刻 repo_exam 還沒做完），暫時不可排。
        assert ready_ids == {"repo_exam"}

        scheduler.mark_task_done(task_list[1], success=True)  # repo_exam 完成
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"svc_common"}  # 全域 tier 0 做完，common 的 service task 放行

    def test_service_task_still_respects_module_dependency(self):
        # module 依賴檢查沒有被整個拿掉：common 這個 module 連一個 task
        # 都還沒動過（狀態還是初始的 "pending"），exam 的 service task
        # 一樣不放行。
        module_list = [
            _module("common"),
            _module("exam", depends_on=["common"]),
        ]
        task_list = [
            _phased_task("svc_exam", "exam", phase=2, layer="services"),
        ]
        scheduler = ModuleScheduler(module_list, task_list)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == set()  # common 還是 pending，exam 的 service task 不放行

    def test_service_task_proceeds_once_dependency_module_starts_progressing(self):
        # 對應 docs/refactor_bug_trace.md #12 的修法：module 依賴只要求
        # upstream「開始有進展」（不是 pending），不要求 upstream 完整
        # verified——只要 common 底下任一個 task 成功，exam 的 service
        # task 就能立刻排進去，不用等 common 走到自己的 router、走到
        # 完整驗證。
        module_list = [
            _module("common"),
            _module("exam", depends_on=["common"]),
        ]
        task_list = [
            _phased_task("svc_common", "common", phase=2, layer="services"),
            _phased_task("svc_exam", "exam", phase=2, layer="services"),
        ]
        scheduler = ModuleScheduler(module_list, task_list)
        scheduler.mark_task_done(task_list[0], success=True)  # common 有一個 task 成功
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"svc_exam"}

    def test_module_dependency_cycle_no_longer_deadlocks(self):
        # 對應 docs/refactor_bug_trace.md #12：真實死結重現。grading 依賴
        # exam；exam 的 router（tier 2）要等全域 Phase 關卡（全專案 tier
        # < 2 都到終態）才放行；但 grading 的 service（tier 1）本身就是
        # 這個「全專案 tier < 2」集合的一份子，修正前它得先等 exam
        # 被判定 "verified"——而 exam 要 "verified"，得先讓自己的 router
        # 跑完。兩邊互相等對方，get_ready_tasks() 永遠回傳空集合，是真正
        # 的死結，不是理論案例（真實 run 20260905_094902_656a0e 裡
        # QuestionService.get_list()／UserProfileRs.from_entity() 就是
        # 這樣卡住，llmlog 查得到零筆呼叫紀錄）。
        module_list = [
            _module("exam"),
            _module("grading", depends_on=["exam"]),
        ]
        task_list = [
            _phased_task("svc_exam", "exam", phase=2, layer="services"),
            _phased_task("router_exam", "exam", phase=2, layer="routers"),
            _phased_task("svc_grading", "grading", phase=2, layer="services"),
        ]
        scheduler = ModuleScheduler(module_list, task_list)

        # exam 的 service 先完成，exam 進入 "in_progress"（不是 "verified"）。
        scheduler.mark_task_done(task_list[0], success=True)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        # 修正前：svc_grading 會卡在 module 依賴（等 exam "verified"，但
        # exam 沒有機會走到 verified，因為它的 router 排不進去）；
        # router_exam 同時卡在全域關卡（等 svc_grading 到終態）——兩邊
        # 互相等對方，這裡會是空集合。修正後：svc_grading 不用等 exam
        # verified，只要 exam 有進展（"in_progress"）就能排進去。
        assert ready_ids == {"svc_grading"}

        # svc_grading 完成後，全專案 tier < 2 全部到終態，全域關卡放行，
        # exam 的 router 這才排得進去——死結被打斷，兩邊都不再互等。
        scheduler.mark_task_done(task_list[2], success=True)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"router_exam"}


class TestBackfillDepsRespectsTranslatorBackend:
    """對應 docs/refactor_bug_trace.md：真實環境重現過一次失敗的 qwen
    repository task 拖垮同 module 底下、跟它毫無關聯的 Claude service／
    router task。`_backfill_missing_task_deps()` 的自動序列鏈原本不分
    `translator_backend`，這裡鎖住修正後的行為：只有 qwen 彼此之間才會
    被自動串鏈，Claude task 不受影響。"""

    def test_failed_qwen_repository_task_does_not_starve_claude_service_task(self):
        module_list = [_module("exam")]
        task_list = [
            _phased_task("repo_1", "exam", phase=1, layer="repositories", translator_backend="qwen"),
            _phased_task("svc_1", "exam", phase=2, layer="services", translator_backend="claude"),
        ]
        scheduler = ModuleScheduler(module_list, task_list)
        scheduler.mark_task_done(task_list[0], success=False)  # repo_1（qwen）永久失敗

        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        # 修正前：svc_1 會被自動串鏈跟在 repo_1 後面，repo_1 進了
        # task_failed（不是 task_done），svc_1 永遠不會就緒。
        assert ready_ids == {"svc_1"}

    def test_two_qwen_tasks_in_same_module_still_auto_chained(self):
        # qwen 併發數鎖死為 1 這個原始理由依然成立——同為 qwen 的 task
        # 之間維持自動序列化，不受這次修正影響。
        module_list = [_module("exam")]
        task_list = [
            _phased_task("repo_1", "exam", phase=1, layer="repositories", translator_backend="qwen"),
            _phased_task("repo_2", "exam", phase=1, layer="repositories", translator_backend="qwen"),
        ]
        scheduler = ModuleScheduler(module_list, task_list)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"repo_1"}  # repo_2 還在排隊

        scheduler.mark_task_done(task_list[0], success=True)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"repo_2"}

    def test_task_missing_translator_backend_not_chained_backward_compatible(self):
        # translator_backend 缺席（NotRequired，只有舊版測試 fixture／01
        # 文件 stub 會缺）時不視為 qwen，不會被自動串鏈——限制最少的
        # 預設值，避免這批既有呼叫端因為這次修正意外改變行為。
        module_list = [_module("exam")]
        task_list = [
            _phased_task("t1", "exam", phase=2, layer="services"),
            _phased_task("t2", "exam", phase=2, layer="services"),
        ]
        scheduler = ModuleScheduler(module_list, task_list)
        ready_ids = {t["id"] for t in scheduler.get_ready_tasks()}
        assert ready_ids == {"t1", "t2"}  # 兩個都立刻就緒，沒有被串成序列
