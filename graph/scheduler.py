from graph.state import ModuleInfo, TaskSpec


class ModuleScheduler:
    """
    依 module_list 的 depends_on 做 topological 排程：
    - 只有「所有依賴 module 都已通過局部驗證」的 module，其 task 才會進入就緒佇列
    - 同一 module 內，task 依 depends_on 序列化（repository → service → router）

    task 級 depends_on 由 [P] Plan Agent 產出，若同 module 內漏填，順序會退化成
    依賴 asyncio.gather 的排程細節（未定義行為）。初始化時做防呆：沒有被引用、
    也沒填 depends_on 的 task，按 task_list 原始順序自動串成序列依賴。
    """

    def __init__(
        self,
        module_list: list[ModuleInfo],
        task_list: list[TaskSpec],
        already_completed: set[str] | None = None,
        already_failed: set[str] | None = None,
        already_verified_modules: set[str] | None = None,
    ):
        """
        `already_*` 讓 scheduler 從前一輪 implement 的執行結果恢復狀態——debug → implement
        是回頭呼叫同一個 node，若每次從零建立 scheduler，module_status 會被重置成全部
        pending，regression 偵測（見 check_upstream_regression）就抓不到已驗證過的 module。
        """
        self.modules = {m["module"]: m for m in module_list}
        self.tasks_by_module: dict[str, list[TaskSpec]] = {}
        for task in task_list:
            self.tasks_by_module.setdefault(task["module"], []).append(task)

        # module 名下擁有哪些檔案，用來偵測「後續寫入是否波及已驗證過的上游 module」（見下方 check_upstream_regression）。
        # 從 task_list 的 target_files 彙整，而不是 Agent ① module_list 的檔名——
        # ① 的檔名只是猜測，③ 架構設計 Agent 可能整個改寫；task_list.target_files
        # 必須是 python_structure.interfaces 中已存在的 file_path（見 graph/state.py
        # TaskSpec 註解），才是這個時間點真正權威的檔案路徑來源。
        self.module_owned_files: dict[str, set[str]] = {}
        for module, tasks in self.tasks_by_module.items():
            self.module_owned_files[module] = {f for t in tasks for f in t["target_files"]}

        self._backfill_missing_task_deps()

        # module 狀態：pending → in_progress → verified / needs_reverify / failed
        self.module_status = {m: "pending" for m in self.modules}
        for m in (already_verified_modules or ()):
            self.module_status[m] = "verified"
        self.task_done: set[str] = set(already_completed or ())
        self.task_failed: set[str] = set(already_failed or ())

    def check_upstream_regression(self, touched_files: list[str], skip_module: str) -> list[str]:
        """回傳已 verified、但這次寫入的檔案剛好落在其名下的 module 清單（排除自己）。
        只在真的觸及已驗證 module 範圍時才觸發，避免每次都全量重驗。
        """
        hit = set()
        for module, owned in self.module_owned_files.items():
            if module == skip_module or self.module_status.get(module) != "verified":
                continue
            if owned & set(touched_files):
                hit.add(module)
        return list(hit)

    def flag_for_reverify(self, module: str):
        """把已驗證 module 打回 needs_reverify——下游依賴它的 module 在重驗通過前不會被釋放。"""
        self.module_status[module] = "needs_reverify"

    def _backfill_missing_task_deps(self):
        """同 module 內沒有 depends_on、也未被引用的 task，依原始順序自動串成序列依賴。
        本地模型併發數鎖死為 1，這些 task 本來就得排隊，強制序列化不拉長總耗時，
        只是換取可預期性與可除錯性。
        """
        for tasks in self.tasks_by_module.values():
            referenced = {dep for t in tasks for dep in t["depends_on"]}
            unordered = [t for t in tasks if not t["depends_on"] and t["id"] not in referenced]
            for prev, curr in zip(unordered, unordered[1:]):
                curr["depends_on"] = curr["depends_on"] + [prev["id"]]

    def _module_deps_satisfied(self, module: str) -> bool:
        deps = self.modules[module]["depends_on"]
        return all(self.module_status.get(d) == "verified" for d in deps)

    def _task_deps_satisfied(self, task: TaskSpec) -> bool:
        return all(dep in self.task_done for dep in task["depends_on"])

    def get_ready_tasks(self) -> list[TaskSpec]:
        """回傳目前可以送去 translator-cli 的 task（尚未考慮模型併發限制）。"""
        ready = []
        for module, status in self.module_status.items():
            if status not in ("pending", "in_progress"):
                continue
            if not self._module_deps_satisfied(module):
                continue
            for task in self.tasks_by_module.get(module, []):
                if task["id"] in self.task_done or task["id"] in self.task_failed:
                    continue
                if self._task_deps_satisfied(task):
                    ready.append(task)
        return ready

    def mark_task_done(self, task: TaskSpec, success: bool):
        if success:
            self.task_done.add(task["id"])
        else:
            self.task_failed.add(task["id"])
        self.module_status[task["module"]] = "in_progress"

    def module_ready_for_verification(self, module: str) -> bool:
        """該 module 底下所有 task 都已完成（成功或失敗）才算齊，觸發局部驗證。"""
        all_tasks = self.tasks_by_module.get(module, [])
        return all(
            t["id"] in self.task_done or t["id"] in self.task_failed
            for t in all_tasks
        ) and len(all_tasks) > 0

    def mark_module_verified(self, module: str, passed: bool):
        self.module_status[module] = "verified" if passed else "failed"

    def all_done(self) -> bool:
        return all(s in ("verified", "failed") for s in self.module_status.values())
