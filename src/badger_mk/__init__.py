#!/usr/bin/env python3
"""
An Inkscape extension to automatically replace values (text, attribute values)
in an SVG file and to then export the result to various file formats.

This is useful e.g. for generating images for name badges and other similar items.
"""
from __future__ import annotations

import argparse
import csv
import logging
import logging.handlers
import os
import shlex
import shutil
import subprocess  # nosec
import sys
import tempfile
import time
import urllib.error
import urllib.parse
from importlib import metadata
from pathlib import Path, PurePosixPath, PureWindowsPath

import cairosvg
import colorlog
import lxml.etree  # nosec  # noqa DUO107
from PyPDF2 import PdfFileMerger

try:
    __version__ = metadata.version("pyPFC")
except metadata.PackageNotFoundError:
    __version__ = None

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument(
        "--version",
        action="version",
        version=str(__version__),
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
        default="",
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


def setup_root_logger() -> logging.Logger:
    logger = logging.getLogger()
    logger.setLevel(logging.NOTSET)

    """
    module_loglevel_map = {
        "pywin32": logger.WARNING,
    }
    for module, loglevel in module_loglevel_map.items():
        logging.getLogger(module).setLevel(loglevel)
    """

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
root_logger = setup_root_logger()


class Badger:
    """Generate image files by replacing variables in the current file"""

    xmlns_map = {
        None: "http://www.w3.org/2000/svg",
        "sodipodi": "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd",
        "svg": "http://www.w3.org/2000/svg",
        "xlink": "http://www.w3.org/1999/xlink",
    }

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

    multipage_formats = [".pdf"]

    def __init__(self) -> None:
        self.tempdir = Path(tempfile.mkdtemp(prefix="badger_"))
        self.out_ext = (
            f".{args.export_type}" if args.export_type else args.export_filename.suffix
        )
        logger.debug("Badger tempdir is '%s'", self.tempdir)

    def __del__(self) -> None:
        logger.debug("Removing tempdir")
        shutil.rmtree(self.tempdir)

    def load(self) -> None:
        with open(self.svg_in_file, "rb") as svgfile:
            self.document = lxml.etree.parse(svgfile)  # nosec

    def process(self) -> None:
        def ns_attrib(namespace: str, attrib: str) -> str:
            ns_attrib = "{" + self.xmlns_map[namespace] + "}" + attrib
            return ns_attrib

        def calc_subst(subst: str) -> str:
            if tag == "text":
                subst = (
                    self.subst_delims[args.subst_mode][0]
                    + subst
                    + self.subst_delims[args.subst_mode][1]
                )

                self.export_filename = Path(
                    str(self.export_filename).replace(subst, value)
                )
            logger.debug(
                "Will replace '%s' in '%s' nodes with '%s'",
                subst,
                tag,
                value,
            )
            return subst

        def subst_in_nodes(tag: str, nodes: list) -> None:
            if tag == "text":
                for node in nodes:
                    for subnode in node.iter():
                        subnode.text = (
                            subnode.text.replace(subst, value)
                            if subnode.text
                            else subnode.text
                        )
                        if node != subnode:
                            subnode.tail = (
                                subnode.tail.replace(subst, value)
                                if subnode.tail
                                else subnode.tail
                            )
            elif tag == "image":
                for node in nodes:
                    if ns_attrib("xlink", "href") in node.attrib:
                        href_fileuri = node.attrib[ns_attrib("xlink", "href")]
                        # Inkscape tends to save relative paths with encoded backslash separators in windows
                        href_filepath = urllib.parse.unquote(
                            urllib.parse.urlparse(href_fileuri).path
                        )
                        href_filepath = Path(PureWindowsPath(href_filepath).as_posix())
                        href_filename = href_filepath.name
                        if subst == href_filename:
                            if ns_attrib("sodipodi", "absref") in node.attrib:
                                node.attrib.pop(ns_attrib("sodipodi", "absref"))
                            node.attrib[ns_attrib("xlink", "href")] = (
                                self.svg_in_file.parent / href_filepath.with_name(value)
                            ).as_uri()

        with open(args.csv_in_file, encoding="utf-8") as csvfile:
            logger.info("Reading CSV data...")
            data = csv.DictReader(
                csvfile,
                dialect="excel",
                delimiter=self.col_delims[args.col_mode],
            )
            logger.debug("Done")

            logger.info("Checking column headers...")
            headers = data.fieldnames
            sanitized_headers = []
            for header in headers:
                logger.debug("Checking column header: '%s'", header)

                if header.count(":") < 1:
                    logger.warning(
                        "Column '%s' will be ignored, as its name is not matching '<tag>:<substition pattern>' format",
                        header,
                    )
                    continue

                logger.debug("Column header OK")
                sanitized_headers += [header]
            logger.debug("Done")

            logger.info("Processing datasets...")
            # Iterate over data rows
            for row in data:
                logger.info("Processing next dataset...")
                logger.debug("row = %s", row)
                self.export_filename = args.export_filename
                self.single_pages = []

                # Iterate over columns for sanitation
                logger.info("Checking row values...")
                sanitized_row = {}
                for header in sanitized_headers:
                    value = row[header]
                    logger.debug("Checking value of: '%s'", header)

                    if not value:
                        logger.warning("No value for header '%s'.", header)
                        continue

                    logger.debug("Column OK")
                    sanitized_row[header] = value
                logger.debug("Done")

                for self.page, self.svg_in_file in enumerate(args.svg_in_files):
                    logger.info(
                        f"Processing page {self.page + 1}/{len(args.svg_in_files)}"
                    )
                    self.load()

                    # Iterate over columns
                    for header, value in sanitized_row.items():  # noqa B007
                        logger.debug("Processing column: '%s'", header)

                        tag, subst = header.split(":", 1)
                        subst = calc_subst(subst)

                        nodes = self.document.iterfind(
                            f".//{tag}",
                            namespaces=self.xmlns_map,
                        )

                        subst_in_nodes(tag, nodes)

                        # TODO: Implement check for missing substitution
                        # logger.warning(f"No replacement for header '{header}'.")

                    self.save()

                if self.out_ext in self.multipage_formats:
                    self.merge()
            logger.debug("Done")

    def save(self) -> int:
        if not self.export_filename.parent.is_dir():
            logger.error("The specified output folder does not exist.")
            return 1

        self.svg_out_dir = (
            self.export_filename.parent if self.out_ext == ".svg" else self.tempdir
        )
        self.page_filename = Path(
            f"{self.export_filename.stem}_{self.page}{self.export_filename.suffix}"
        )

        logger.info(
            "Saving file as '%s'",
            self.svg_out_dir / self.page_filename.with_suffix(".svg"),
        )
        with open(
            self.svg_out_dir / self.page_filename.with_suffix(".svg"), "wb"
        ) as file:
            self.document.write(
                file, encoding="utf-8", xml_declaration=True, standalone=False
            )  # pretty_print=True screws svg

        if True:  # Convert text to path
            if not shutil.which("inkscape"):
                # TODO: Check at beginning of script
                logger.error("Converting text to path requires inkscape in PATH...")
            else:
                logger.info("Converting text to path")
                cmd = "inkscape {io_file} --export-text-to-path --export-overwrite --export-filename={io_file}".format(
                    io_file=self.svg_out_dir / self.page_filename.with_suffix(".svg")
                )
                inkscape_rc = subprocess.run(  # nosec
                    shlex.split(cmd, posix=(os.name == "posiX"))
                )

            if inkscape_rc.returncode:
                logger.error(
                    "Inkscape exited with returncode %s", inkscape_rc.returncode
                )

        if self.out_ext != ".svg":
            self.convert()

    def convert(self) -> None:
        if self.out_ext in self.multipage_formats:
            convert_out_dir = self.tempdir
            self.single_pages += [
                convert_out_dir / self.page_filename.with_suffix(self.out_ext)
            ]
        else:
            convert_out_dir = self.export_filename.parent
        logger.info(
            "Converting file to '%s'",
            convert_out_dir / self.page_filename.with_suffix(self.out_ext),
        )

        try:
            if self.out_ext == ".pdf":
                cairosvg.svg2pdf(
                    url=str(self.svg_out_dir / self.page_filename.with_suffix(".svg")),
                    write_to=str(
                        convert_out_dir / self.page_filename.with_suffix(self.out_ext)
                    ),
                )
            elif self.out_ext == ".png":
                cairosvg.svg2png(
                    url=str(self.svg_out_dir / self.page_filename.with_suffix(".svg")),
                    write_to=str(convert_out_dir / self.page_filename),
                    dpi=args.export_dpi,
                )
        except urllib.error.URLError as exc:
            logger.error(
                "Missing linked image, export file might be missing: %s", exc.reason
            )  # TODO: Read filename

    def merge(self) -> None:
        if self.out_ext == ".pdf":
            merger = PdfFileMerger()

            logger.info("Merging pages to '%s'", self.export_filename)
            for single_page in self.single_pages:
                merger.append(str(single_page))
            merger.write(str(self.export_filename))
            merger.close()

    def run(self) -> None:
        self.process()
