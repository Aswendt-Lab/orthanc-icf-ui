from pathlib import Path
from tempfile import gettempdir
from zipfile import ZipFile

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import (
    Header,
    Footer,
    Input,
    SelectionList,
    Button,
    RichLog,
    ProgressBar,
)
from textual.worker import Worker, WorkerState

import time
import asyncio
import aiofiles
import httpx
import subprocess

from rich.text import Text


class OrthancClient:
    def __init__(self):
        self.baseurl = "http://localhost:8042"
        self.client = None

    async def login(self, user, password) -> None:
        """Check if credentials work and remember them

        There is no actual log in action, so we perform a cheap call
        (get system information). If it works, user and pass will be
        used for other calls. Raises an error if it fails.

        """
        if user == "" and password == "":
            self.client = httpx.AsyncClient()
        else:
            self.client = httpx.AsyncClient(auth=(user, password))

        response = await self.client.get(f"{self.baseurl}/system")
        response.raise_for_status()

    async def query(self, date: str) -> list:
        d = {
            "Level": "Study",
            "Query": {
                "StudyDate": date,
            },
        }

        response = await self.client.post(
            url=f"{self.baseurl}/tools/find",
            json=d,
        )
        response.raise_for_status()
        matched_ids = response.json()

        results = []
        for study_id in matched_ids:
            response = await self.client.get(f"{self.baseurl}/studies/{study_id}")
            d = response.json()
            patientID = d.get("PatientMainDicomTags", {}).get("PatientID")
            results.append((patientID, study_id))  # orthanc study id

        return results

    async def export(self, study_id: str) -> Path:
        outdir = Path(gettempdir())
        zip_path = outdir.joinpath(f"{study_id}.zip")
        out_path = outdir.joinpath(f"{study_id}")

        async with self.client.stream(
            "GET", f"{self.baseurl}/studies/{study_id}/archive"
        ) as r:
            async with aiofiles.open(zip_path, "wb") as f:
                async for chunk in r.aiter_bytes(chunk_size=10 * 1024):
                    await f.write(chunk)

        with ZipFile(zip_path) as zf:
            zf.extractall(out_path)

        zip_path.unlink()

        return out_path


class OrthancApp(App):
    BINDINGS = [("q", "quit", "Quit")]
    CSS_PATH="style.tcss"

    def __init__(self):
        super().__init__()
        self.orthanc = OrthancClient()

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()
        with Horizontal(classes="onerow"):
            yield Input(placeholder="Username", id="user_input", classes="column")
            yield Input(placeholder="Password", id="password_input", classes="column", password=True)
            yield Button("Connect", id="connect_button")

        # yield LoginBox(classes="onerow")
        yield Input(placeholder="Study date", id="date_input", disabled=True)
        yield SelectionList(id="sel_list")
        yield Button("Export", id="export_button", disabled=True)
        yield RichLog(highlight=True, markup=True)
        yield Footer()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "date_input":
            sl = self.get_child_by_id("sel_list")
            sl.clear_options()

            if event.input.value != "":
                # orthanc sees "" as "any", but we are different
                self.run_worker(self.orthanc.query(event.input.value), name="query", exclusive=True, exit_on_error=False)

    async def call_icf(self, cmd: str, *args) -> None:
        icf_image = Path.home() / "Documents" / "inm-icf-utilities" / "icf.sif"

        log = self.query_one(RichLog)

        # ignore stdin/out for now, just wait for process to exit
        proc = await asyncio.create_subprocess_exec(
            icf_image,
            cmd,
            *args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        returncode = await proc.wait()

        if returncode == 0:
            msg = "[green]OK[/green]"
        else:
            msg = f"[red]ERROR {returncode}[/red]"

        log.write(f"({msg}) {cmd}")

    async def do_stuff(self, subject_ids: list):
        log = self.query_one(RichLog)

        for s in subject_ids:
            log.write(f"Processing dicom study id {s}")

            # export dicoms from orthanc
            dicom_dir = await self.orthanc.export(s)
            log.write(f"Exported {dicom_dir}")

            # TODO: make paths depend on input
            result = await self.call_icf(
                "make_studyvisit_archive",
                "--output-dir",
                "/tmp/store",
                "--id",
                "study-id",
                "visit-id",
                dicom_dir,
            )

            # TODO: following icf steps

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "connect_button":
            user = self.get_widget_by_id("user_input").value
            password = self.get_widget_by_id("password_input").value
            self.run_worker(self.orthanc.login(user, password), name="login", exclusive=True, exit_on_error=False)

        if event.button.id == "export_button":
            sl = self.get_child_by_id("sel_list")
            self.run_worker(self.do_stuff(sl.selected), exclusive=True)


    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        log = self.query_one(RichLog)

        # login to orthanc (credentials check)
        if event.worker.name == "login":
            date_input = self.get_widget_by_id("date_input")
            if event.state == WorkerState.SUCCESS:
                log.write("Connected successfully")
                date_input.disabled = False

        # query orthanc
        elif event.worker.name == "query":
            if event.state == WorkerState.SUCCESS:
                sl = self.get_child_by_id("sel_list")
                sl.add_options(event.worker.result)

        # do not let errors pass unnoticed
        if event.state == WorkerState.ERROR:
            log.write(event)
            log.write(event.worker.error)


    def on_selection_list_selected_changed(
        self, event: SelectionList.SelectedChanged
    ) -> None:
        """Enable export button when at least one subject selected"""

        btn = self.get_child_by_id("export_button")
        if len(event.selection_list.selected) > 0:
            btn.disabled = False
        else:
            btn.disabled = True


if __name__ == "__main__":
    app = OrthancApp()
    app.run()
