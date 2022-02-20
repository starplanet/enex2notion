import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from notion.client import NotionClient

from enex2notion.cli_wkhtmltopdf import ensure_wkhtmltopdf
from enex2notion.enex_parser import iter_notes
from enex2notion.enex_uploader import (
    NoteUploadFailException,
    get_import_root,
    upload_note,
)
from enex2notion.enex_uploader_modes import get_notebook_database, get_notebook_page
from enex2notion.note_parser import parse_note
from enex2notion.version import __version__

logger = logging.getLogger(__name__)


class DoneFile(object):
    def __init__(self, path: Path):
        self.path = path

        try:
            with open(path, "r") as f:
                self.done_hashes = {line.strip() for line in f}
        except FileNotFoundError:
            self.done_hashes = set()

    def __contains__(self, note_hash):
        return note_hash in self.done_hashes

    def add(self, note_hash):
        self.done_hashes.add(note_hash)

        with open(self.path, "a") as f:
            f.write(f"{note_hash}\n")


def _upload_note(notebook_root, note, note_blocks):
    for attempt in range(5):
        try:
            upload_note(notebook_root, note, note_blocks)
        except NoteUploadFailException:
            if attempt == 4:
                raise
            logger.warning(
                f"Failed to upload note '{note.title}' to Notion! Retrying..."
            )
            continue
        break


class EnexUploader(object):
    def __init__(
        self,
        import_root,
        mode: str,
        mode_webclips: str,
        done_file: Path,
        add_meta: bool,
        add_pdf_preview: bool,
    ):
        self.import_root = import_root
        self.mode = mode
        self.mode_webclips = mode_webclips
        self.done_hashes = DoneFile(done_file) if done_file else set()
        self.add_meta = add_meta
        self.add_pdf_preview = add_pdf_preview

    def upload(self, enex_file: Path):
        logger.info(f"Processing notebook '{enex_file.stem}'...")

        notebook_root = self._get_notebook_root(enex_file.stem)

        for note in iter_notes(enex_file):
            if note.note_hash in self.done_hashes:
                logger.debug(f"Skipping note '{note.title}' (already uploaded)")
                continue

            logger.debug('start to parse note "%s"', note.title)
            note_blocks = self._parse_note(note)
            if not note_blocks:
                continue

            if notebook_root is not None:
                _upload_note(notebook_root, note, note_blocks)
                self.done_hashes.add(note.note_hash)

    def _parse_note(self, note):
        try:
            return parse_note(
                note,
                mode_webclips=self.mode_webclips,
                is_add_meta=self.add_meta,
                is_add_pdf_preview=self.add_pdf_preview,
            )
        except Exception:
            logger.error(f"Unhandled exception while parsing note '{note.title}'!")
            raise

    def _get_notebook_root(self, notebook_title):
        if self.import_root is None:
            return None

        if self.mode == "DB":
            return get_notebook_database(self.import_root, notebook_title)

        return get_notebook_page(self.import_root, notebook_title)


def cli(argv):
    args = parse_args(argv)

    _setup_logging(args.verbose, args.log)

    if args.mode_webclips == "PDF":
        ensure_wkhtmltopdf()

    if args.token:
        root = get_import_root(
            NotionClient(token_v2=args.token), "Evernote ENEX Import"
        )
    else:
        logger.warning(
            "No token provided, dry run mode. Nothing will be uploaded to Notion!"
        )
        root = None

    enex_uploader = EnexUploader(
        import_root=root,
        mode=args.mode,
        mode_webclips=args.mode_webclips,
        done_file=args.done_file,
        add_meta=args.add_meta,
        add_pdf_preview=args.add_pdf_preview,
    )

    for enex_input in args.enex_input:
        if enex_input.is_dir():
            logger.info(f"Processing directory '{enex_input.name}'...")
            for enex_file in sorted(enex_input.glob("**/*.enex")):
                enex_uploader.upload(enex_file)
        else:
            enex_uploader.upload(enex_input)


def main():  # pragma: no cover
    try:
        cli(sys.argv[1:])
    except KeyboardInterrupt:
        sys.exit(1)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="enex2notion", description="Uploads ENEX files to Notion"
    )

    schema = {
        "enex_input": {
            "type": Path,
            "nargs": "+",
            "help": "ENEX files or directories to upload",
            "metavar": "FILE/DIR",
        },
        "--token": {
            "type": str,
            "help": (
                "Notion token, stored in token_v2 cookie for notion.so"
                " [NEEDED FOR UPLOAD]"
            ),
        },
        "--mode": {
            "choices": ["DB", "PAGE"],
            "default": "DB",
            "help": (
                "upload each ENEX as database (DB) or page with children (PAGE)"
                " (default: DB)"
            ),
        },
        "--mode-webclips": {
            "choices": ["TXT", "PDF"],
            "default": "TXT",
            "help": (
                "convert web clips to text (TXT) or pdf (PDF) before upload"
                " (default: TXT)"
            ),
        },
        "--add-pdf-preview": {
            "action": "store_true",
            "default": False,
            "help": (
                "include preview image with PDF webclips for gallery view thumbnail"
                " (works only with --mode-webclips=PDF)"
            ),
        },
        "--add-meta": {
            "action": "store_true",
            "default": False,
            "help": (
                "include metadata (created, tags, etc) in notes,"
                " makes sense only with PAGE mode"
            ),
        },
        "--done-file": {
            "type": Path,
            "metavar": "FILE",
            "help": "file for uploaded notes hashes to resume interrupted upload",
        },
        "--log": {
            "type": Path,
            "metavar": "FILE",
            "help": "file to store program log",
        },
        "--verbose": {
            "action": "store_true",
            "default": False,
            "help": "output debug information",
        },
        "--version": {
            "action": "version",
            "version": f"%(prog)s {__version__}",  # noqa: WPS323
        },
    }

    for arg, arg_params in schema.items():
        parser.add_argument(arg, **arg_params)

    return parser.parse_args(argv)


def _setup_logging(is_verbose: bool, log_file: Optional[Path]):
    logging.basicConfig(format="%(levelname)s: %(message)s")

    logging.getLogger("enex2notion").setLevel(
        logging.DEBUG if is_verbose else logging.INFO
    )

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)-8.8s] %(message)s")
        )
        logging.getLogger("enex2notion").addHandler(file_handler)

    logging.getLogger("urllib3").setLevel(logging.ERROR)
