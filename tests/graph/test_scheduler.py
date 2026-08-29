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
