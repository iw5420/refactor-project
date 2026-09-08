# tests/test_main.py
"""main.py 的 pipeline crash rollback 銜接，對應 docs/refactor_bug_trace.md
#31：main() 這一輪若整個 crash（連 write_run_report() 都沒執行到，例如
真實案例 `20260906_060030_4019f4` 的 API 額度不足），`reset_python_
project_dir()` 建立的新目錄只是半成品，不該留著取代上一輪的真實產物。

用真實檔案系統＋monkeypatch `main._run_pipeline()` 模擬崩潰——不真的跑
一次完整 pipeline（成本高、慢，而且真的讓 API 額度用盡才能重現不可控），
只驗證 `main()` 自己的 try/except 銜接邏輯：crash 時是否正確呼叫
`rollback_python_project_dir()`、還原上一輪內容、重新拋出例外。跟
`asyncio.run(...)` 包在一般 `def test_xxx()` 裡的既有慣例一致（見
`tests/graph/test_give_up_node.py` 等），不用 pytest-asyncio 標記。
"""
import asyncio

import pytest

import main as main_module


@pytest.fixture(autouse=True)
def _no_real_logging_side_effects(monkeypatch):
    # main() 呼叫的 configure_logging(file_handler=True) 預設會在真實
    # repo 底下寫 logs/orchestrator.log——這裡只測 rollback 銜接邏輯，
    # 不需要真的裝 log handler，直接讓它變成 no-op 避免污染 repo 狀態。
    monkeypatch.setattr(main_module, "configure_logging", lambda *args, **kwargs: None)


class TestMainRollsBackOnPipelineCrash:
    def test_crash_restores_previous_run_content_and_reraises(self, tmp_path, monkeypatch):
        project = tmp_path / "exam-platform-api"
        project.mkdir()
        (project / "real_work.py").write_text("real = True\n", encoding="utf-8")
        monkeypatch.setenv("PYTHON_PROJECT_PATH", str(project))
        monkeypatch.setenv("JAVA_PROJECT_PATH", str(tmp_path / "java-project"))

        async def _boom(python_project_path: str, run_id: str) -> None:
            # 模擬 pipeline 執行到一半、已經寫出一些半成品內容之後才 crash
            # （例如 scaffold 已經跑過，但 API 額度在填空階段中途用盡）。
            (project / "half_finished.py").write_text("half = True\n", encoding="utf-8")
            raise RuntimeError("simulated crash: API 額度不足")

        monkeypatch.setattr(main_module, "_run_pipeline", _boom)

        with pytest.raises(RuntimeError, match="simulated crash"):
            asyncio.run(main_module.main())

        # 目錄還原成上一輪的真實內容，半成品消失，沒有殘留的備份資料夾。
        assert project.is_dir()
        assert (project / "real_work.py").read_text(encoding="utf-8") == "real = True\n"
        assert not (project / "half_finished.py").exists()
        assert list(tmp_path.glob("exam-platform-api.bak-*")) == []

    def test_crash_with_no_previous_run_just_removes_half_finished_dir(self, tmp_path, monkeypatch):
        # 這一輪開始前 python_project_path 本來就不存在（例如第一次跑）
        # ——reset 沒有東西可備份，crash 時只需要刪掉這次的半成品。
        project = tmp_path / "exam-platform-api"
        monkeypatch.setenv("PYTHON_PROJECT_PATH", str(project))
        monkeypatch.setenv("JAVA_PROJECT_PATH", str(tmp_path / "java-project"))

        async def _boom(python_project_path: str, run_id: str) -> None:
            (project / "half_finished.py").write_text("half = True\n", encoding="utf-8")
            raise RuntimeError("simulated crash")

        monkeypatch.setattr(main_module, "_run_pipeline", _boom)

        with pytest.raises(RuntimeError, match="simulated crash"):
            asyncio.run(main_module.main())

        assert not project.exists()

    def test_successful_run_leaves_new_content_in_place(self, tmp_path, monkeypatch):
        # 對照組：pipeline 正常跑完（不論成功或 give_up，這裡只模擬
        # _run_pipeline() 正常返回），不該觸發 rollback，新目錄的內容
        # 要維持原樣。
        project = tmp_path / "exam-platform-api"
        project.mkdir()
        (project / "previous_run.py").write_text("previous = True\n", encoding="utf-8")
        monkeypatch.setenv("PYTHON_PROJECT_PATH", str(project))
        monkeypatch.setenv("JAVA_PROJECT_PATH", str(tmp_path / "java-project"))

        async def _succeeds(python_project_path: str, run_id: str) -> None:
            (project / "new_run_output.py").write_text("done = True\n", encoding="utf-8")

        monkeypatch.setattr(main_module, "_run_pipeline", _succeeds)

        asyncio.run(main_module.main())

        assert (project / "new_run_output.py").read_text(encoding="utf-8") == "done = True\n"
        assert not (project / "previous_run.py").exists()  # 已被搬進備份，不在新目錄裡
        backups = list(tmp_path.glob("exam-platform-api.bak-*"))
        assert len(backups) == 1
        assert (backups[0] / "previous_run.py").read_text(encoding="utf-8") == "previous = True\n"
