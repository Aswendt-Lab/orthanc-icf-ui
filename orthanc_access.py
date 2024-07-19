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
from textual.worker import Worker

import requests

import time
import asyncio
import aiofiles
import httpx
import subprocess

from rich.text import Text


def query_orthanc(date: str, user: str, password: str) -> list:
    baseurl = "http://localhost:8042"

    d = {
        "Level": "Study",
        "Query": {
            "StudyDate": date,
        },
    }

    r = requests.post(
        url=f"{baseurl}/tools/find",
        json=d,
        auth=(user, password),
    )

    if not r.ok:
        r.raise_for_status()

    matched_ids = r.json()

    results = []
    for study_id in matched_ids:
        r = requests.get(f"{baseurl}/studies/{study_id}", auth=(user, password))
        d = r.json()
        patientID = d.get("PatientMainDicomTags", {}).get("PatientID")
        results.append((patientID, study_id))  # orthanc study id

    return results


async def export_zipfile(study_id: str) -> None:
    baseurl = "http://localhost:8042"
    outdir = Path(gettempdir())

    zip_path = outdir.joinpath(f"{study_id}.zip")
    out_path = outdir.joinpath(f"{study_id}")

    client = httpx.AsyncClient()
    async with client.stream("GET", f"{baseurl}/studies/{study_id}/archive") as r:
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

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()
        with Horizontal(classes="onerow"):
            yield Input(placeholder="Username", id="user_input", classes="column")
            yield Input(placeholder="Password", id="password_input", classes="column", password=True)

        # yield LoginBox(classes="onerow")
        yield Input(placeholder="Study date", id="date_input")
        yield SelectionList(id="sel_list")
        yield Button("Export", id="export_button", disabled=True)
        yield RichLog(highlight=True, markup=True)
        yield Footer()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "date_input":
            sl = self.get_child_by_id("sel_list")
            sl.clear_options()

            user = self.get_widget_by_id("user_input").value
            password = self.get_widget_by_id("password_input").value

            if event.input.value != "":
                # orthanc sees "" as "any", but we are different
                try:
                    res = query_orthanc(event.input.value, user, password)
                except requests.exceptions.HTTPError as e:
                    log = self.query_one(RichLog)
                    log.write(e)
                    res = []
                sl.add_options(res)

    async def mock_call(self, cmd):
        log = self.query_one(RichLog)
        await asyncio.sleep(2)
        log.write(f"Completed {cmd}")
        return 1

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
            dicom_dir = await export_zipfile(s)
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
        if event.button.id == "export_button":
            sl = self.get_child_by_id("sel_list")
            log = self.query_one(RichLog)

            self.run_worker(self.do_stuff(sl.selected), exclusive=True)

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
