#!/usr/bin/env python3
#
# badger - an Inkscape extension to export images with automatically replaced values
# Copyright (C) 2008  Aurélio A. Heckert (original Generator extension in Bash)
#               2019  Maren Hachmann (Python rewrite, update for Inkscape 1.0)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
# Version 0.9
"""
An Inkscape extension to automatically replace values (text, attribute values)
in an SVG file and to then export the result to various file formats.

This is useful e.g. for generating images for name badges and other similar items.
"""


import argparse
import csv
import logging
import logging.handlers
import os
import shlex
import subprocess  # nosec
import sys
import tempfile
from pathlib import Path

import colorlog

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path(".")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    logging_grp = parser.add_argument_group(title="Logging")
    logging_grp.add_argument(
        "-v",
        "--verbosity",
        default="INFO",
        type=str.upper,
        choices=list(logging._nameToLevel.keys()),
        help="Console log level",
    )
    logging_grp.add_argument(
        "-L",
        "--log",
        default="DEBUG",
        type=str.upper,
        choices=list(logging._nameToLevel.keys()),
        help="File log level",
    )

    output_grp = parser.add_argument_group(title="Output")
    output_grp.add_argument(
        "-o",
        "--export-filename",
        dest="export_filename",
        required=True,
        type=Path,
        help="File path for output (include placeholders!)",
    )
    output_grp.add_argument(
        "--export-type",
        dest="export_type",
        choices=["eps", "pdf", "png", "ps", "svg"],  # self.actions.keys(),
        default="pdf",
        help="File format to export to",
    )
    output_grp.add_argument(
        "-D",
        "--export-dpi",
        dest="export_dpi",
        type=int,
        default=300,
        help="Resolution for exported raster images in dpi",
    )

    input_grp = parser.add_argument_group(title="Input")
    input_grp.add_argument(
        "-C",
        "--col-mode",
        dest="col_mode",
        type=str,
        choices=["comma", "semicolon", "tab"],  # self.col_delims.keys(),
        default="comma",
        help="Substitution mode csv delimiter",
    )
    input_grp.add_argument(
        "-s",
        "--subst-mode",
        dest="subst_mode",
        type=str,
        choices=["jinja", "shell", "win"],  # self.subst_delims.keys(),
        default="jinja",
        help="Substitution mode",
    )
    input_grp.add_argument(
        "-c",
        "--csv-in",
        dest="csv_in_file",
        required=True,
        type=Path,
        help="Path to data file",
    )
    input_grp.add_argument(
        dest="svg_in_files",
        metavar="graphics-file",
        nargs="+",
        type=Path,
        help="Path to graphic file",
    )

    return parser.parse_args()


def setup_root_logger(path: Path = DEFAULT_LOG_PATH) -> logging.Logger:
    global logfile_path

    logger = logging.getLogger()
    logger.setLevel(logging.NOTSET)

    """
    module_loglevel_map = {
        "pywin32": logger.WARNING,
    }
    for module, loglevel in module_loglevel_map.items():
        logging.getLogger(module).setLevel(loglevel)
    """

    logfile_path = Path(f"{Path(path) / Path(__file__).stem}.log")
    log_roll = logfile_path.is_file()
    file_handler = logging.handlers.RotatingFileHandler(
        filename=logfile_path, mode="a", backupCount=9, encoding="utf-8",
    )
    if log_roll:
        file_handler.doRollover()
    file_handler.setLevel(args.log)
    file_handler.setFormatter(
        logging.Formatter(
            fmt="[%(asctime)s.%(msecs)03d][%(name)s:%(levelname).4s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logger.addHandler(file_handler)

    console_handler = colorlog.StreamHandler()
    console_handler.setLevel(args.verbosity)
    console_handler.setFormatter(
        colorlog.ColoredFormatter(
            fmt="[%(bold_blue)s%(name)s%(reset)s:%(log_color)s%(levelname).4s%(reset)s] %(msg_log_color)s%(message)s",
            log_colors={
                "DEBUG": "fg_bold_cyan",
                "INFO": "fg_bold_green",
                "WARNING": "fg_bold_yellow",
                "ERROR": "fg_bold_red",
                "CRITICAL": "fg_thin_red",
            },
            secondary_log_colors={
                "msg": {
                    "DEBUG": "fg_white",
                    "INFO": "fg_bold_white",
                    "WARNING": "fg_bold_yellow",
                    "ERROR": "fg_bold_red",
                    "CRITICAL": "fg_thin_red",
                },
            },
        )
    )
    logger.addHandler(console_handler)

    if False:
        # List all log levels with their respective coloring
        for log_lvl_name, log_lvl in logging._nameToLevel.items():
            logger.log(log_lvl, "This is test message for %s", log_lvl_name)

    return logger


args = parse_args()
TempDir = Path(tempfile.mkdtemp(prefix="badger_"))
root_logger = setup_root_logger(path=TempDir)


class Badger:
    """Generate image files by replacing variables in the current file"""

    subst_delims = {
        "jinja": (r"{{ ", r" }}"),
        "win": (r"%", r"%"),
        "shell": (r"${", r"}"),
    }

    col_delims = {
        "comma": (r","),
        "semicolon": (r";"),
        "tab": (r"\t"),
    }

    actions = {
        "eps": "inkscape --export-dpi={dpi} --export-text-to-path --export-filename={file_out} {file_in}",
        "png": "inkscape --export-dpi={dpi} --export-filename={file_out} {file_in}",
        "pdf": "inkscape --export-dpi={dpi} --export-pdf-version=1.5 --export-text-to-path --export-filename={file_out} {file_in}",
        "ps": "inkscape --export-dpi={dpi} --export-text-to-path --export-filename={file_out} {file_in}",
        "svg": "",
    }

    multipage_formats = [".pdf"]

    def __init__(self):
        self.tempdir = Path(tempfile.mkdtemp(prefix="badger_"))

    def __del__(self):
        print("WOULD BE DELETING TEMPDIR NOW")
        # shutil.rmtree(self.tempdir)

    def load(self):
        with open(self.svg_in_file, "r", encoding="utf-8") as svgfile:
            self.document = svgfile.read()

    def process(self):
        self.load()
        with open(args.csv_in_file, "r", encoding="utf-8") as csvfile:
            data = csv.DictReader(
                csvfile, dialect="excel", delimiter=self.col_delims[args.col_mode],
            )
            for row in data:
                logger.info(f"Processing row {row}")

                self.new_doc = self.document

                self.export_filename = args.export_filename

                for key, value in row.items():
                    if key[0] + key[-1] == "<>":
                        key_subst = key.strip("<>")
                    else:
                        key_subst = (
                            self.subst_delims[args.subst_mode][0]
                            + key
                            + self.subst_delims[args.subst_mode][1]
                        )

                        self.export_filename = Path(
                            str(self.export_filename).replace(key_subst, value)
                        )
                    self.new_doc = self.new_doc.replace(key_subst, value)

                    if not value:
                        logger.warning(f"No value for key '{key}'.")

                    if self.new_doc == self.document:
                        logger.warning(f"No replacement for key '{key}'.")

                self.save()

    def save(self):

        page_filename = TempDir / Path(
            f"{self.export_filename.stem}_{self.page}{self.export_filename.suffix}"
        )

        if self.export_filename.suffix in self.multipage_formats:
            self.page_filename_map[self.export_filename] = page_filename

        self.export_filename = page_filename

        if not self.export_filename.parent.is_dir():
            logger.error("The selected output folder does not exist.")
            return True

        if args.export_type == "svg":
            with open(self.export_filename, "w", encoding="utf-8") as file:
                file.write(self.new_doc)
        else:
            temp_svg_file = TempDir / self.export_filename.with_suffix(".svg")
            with open(temp_svg_file, "w", encoding="utf-8") as file:
                file.write(self.new_doc)

            cmd = self.actions[args.export_type].format(
                dpi=args.export_dpi,
                file_out=self.export_filename,
                file_in=temp_svg_file,
            )
            ret = subprocess.run(  # nosec
                shlex.split(cmd, posix=(os.name == "posix")),
                stdout=sys.stdout,
                stderr=sys.stderr,
            ).returncode
            if ret:
                logger.error(f"Inkscape return code {ret}")

    def merge(self):
        # if args.export_type == "pdf":
        if self.export_filename.suffix in self.multipage_formats:
            print(f"Merge {self.page_filename_map} to {self.export_filename}")
            # delete Temp Dir with Files?

    def run(self):
        self.page_filename_map = {}
        for self.page, self.svg_in_file in enumerate(args.svg_in_files):
            logger.info(f"Processing page {self.page + 1}/{len(args.svg_in_files)}")
            self.process()
        self.merge()


logger.debug(args)


def main():
    """Return codes:
    1: Not auto-merged
    2: Unknown file extension
    3: COM Application (Word, PowerPoint) not installed
    4: File not found
    5: Unknown git lfs pointer --check return code
    6: Unexpected pywin32 com_error
    """
    logger.debug(
        "Badger is logging to '%s'", logfile_path,
    )


Badger().run()
