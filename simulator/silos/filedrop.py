"""
filedrop.py  (a folder somebody drops files into, which is how small
businesses actually integrate)

More common than any DATABASE connection. A small business's bank
statement arrives as a CSV download. Its supplier's price list arrives
as a spreadsheet by email. Its payroll provider drops a file on a
share every fortnight. Its card processor publishes a settlement file
nightly. None of that is a database and all of it is real operational
data that a platform ends up reading -- which is why Foundry lists
plain directories, SMB shares, SFTP and FTP alongside its database
connectors.

What this silo is: a directory, and a discipline about how files
appear in it.

The discipline is the interesting part, and it is the thing a naive
implementation gets wrong. A consumer polling a folder reads whatever
is there when it looks. If a file is written in place over several
seconds, the consumer can and eventually will read half of it -- a CSV
truncated mid-row, parsed without complaint into a short file with a
mangled last record. So well-behaved publishers write to a temporary
name and rename into place, because rename within a filesystem is
atomic and a consumer therefore sees the file either not at all or
complete.

And not every real publisher does this. Plenty write in place, and the
resulting torn reads are a genuine, recurring integration failure.
`place()` is atomic by default and takes `atomic=False` so that
condition can be produced deliberately -- it is a property of the
upstream being simulated, not a test hook.

CSV dialect defaults to what these files really look like: CRLF line
endings and a UTF-8 BOM, because the overwhelming majority are
produced by, or intended to be opened in, Excel on Windows. Excel
misreads a UTF-8 file without the BOM, so exporters emit one, and a
consumer that assumes clean UTF-8 finds three stray bytes on the first
field name. Defaulting to the tidy form would be simulating a file
nobody sends.
"""

import csv
import io
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import ClassVar

from simulator.silo import ConnectionDescriptor, Silo, SiloError
from simulator.silos.nodatabase import refuse_database

#: Byte order mark. Excel needs it to read UTF-8 correctly, so real
#: exports carry it, so consumers have to cope with it.
UTF8_BOM = "\ufeff"

#: Suffix used while a file is being written, before the rename that
#: publishes it. Chosen to be obviously incomplete to anything that
#: lists the directory, and to be a suffix rather than a prefix so a
#: consumer filtering on `*.csv` skips it naturally.
PARTIAL_SUFFIX = ".part"

#: What a terminated silo's directory is renamed to. Recoverable: a
#: share going away is usually a mount dropping or a permission
#: change, not a deletion.
TERMINATED_SUFFIX = ".gone"


class FileDropSilo(Silo):
    """One silo that is a directory files are published into."""

    kind: ClassVar[str] = "filedrop"
    #: A folder listens on nothing.
    requires_port: ClassVar[bool] = False

    def __init__(self, name: str, data_dir: Path, folder: str | None = None,
                 *, bom: bool = True, line_terminator: str = "\r\n") -> None:
        super().__init__(name, data_dir)
        #: Named after the silo by default, so a world's directory
        #: reads as a list of systems rather than of folders.
        self.folder = folder or name
        self.bom = bom
        self.line_terminator = line_terminator

    @property
    def path(self) -> Path:
        return self.data_dir / self.folder

    # -- lifecycle ---------------------------------------------------

    def create(self) -> None:
        if self.path.exists():
            raise SiloError(
                f"{self.name}: {self.path} already exists; remove the world's "
                f"directory to rebuild it"
            )
        self.path.mkdir(parents=True)

    def start(self) -> None:
        """Nothing to start. A directory is readable or it is not."""
        return None

    def stop(self) -> None:
        """Nothing to stop. See start()."""
        return None

    def is_reachable(self) -> bool:
        """Whether the directory exists and can actually be used.

        Readability is checked as well as existence, because the
        realistic way a share becomes unusable is a permission change
        or a dropped mount, not a deletion -- and in both of those the
        path can still be there.
        """
        return self.path.is_dir() and os.access(self.path, os.R_OK | os.X_OK)

    def connection(self, database: str | None = None) -> ConnectionDescriptor:
        refuse_database(self.name, self.kind, database)
        """A path, and the format a consumer should expect to find."""
        return ConnectionDescriptor(kind=self.kind, details={
            "path": str(self.path),
            "format": "csv",
            "encoding": "utf-8-sig" if self.bom else "utf-8",
        })

    def terminate(self) -> None:
        """Move the folder aside, so the share abruptly stops existing."""
        if self.path.exists():
            self.path.replace(self.path.with_name(self.path.name + TERMINATED_SUFFIX))

    # -- publishing --------------------------------------------------

    def place(self, filename: str, content: str, *, atomic: bool = True) -> Path:
        """Publish one file.

        Atomic by default: written under a partial name and renamed
        into place, so a consumer polling the directory sees the file
        either absent or complete. `atomic=False` writes in place,
        which is what a badly-behaved real publisher does and which
        produces torn reads -- a condition worth being able to cause on
        purpose rather than only suffer.
        """
        if "/" in filename or "\\" in filename:
            # A publisher drops files in the folder. A name with a
            # separator in it is a bug in the caller, and allowing it
            # would let a pack write outside its own silo.
            raise SiloError(f"{self.name}: {filename!r} must be a plain file name")
        if not self.is_reachable():
            raise SiloError(f"{self.name}: {self.path} is not reachable")

        target = self.path / filename
        payload = (UTF8_BOM + content) if self.bom else content

        if not atomic:
            target.write_text(payload, encoding="utf-8", newline="")
            return target

        partial = target.with_name(target.name + PARTIAL_SUFFIX)
        partial.write_text(payload, encoding="utf-8", newline="")
        # Rename within one filesystem is atomic, which is the whole
        # mechanism: the directory entry appears complete or not at all.
        partial.replace(target)
        return target

    def write_csv(self, filename: str, header: Sequence[str],
                  rows: Iterable[Sequence[object]], *, atomic: bool = True) -> Path:
        """Publish a CSV in the dialect these files really use.

        csv.writer is given the line terminator explicitly and the
        result is written with newline="" so Python does not translate
        the terminators a second time. Getting that wrong produces
        CRLFCRLF, which most parsers tolerate and some do not -- a
        difference that only shows up on a consumer nobody tested with.
        """
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator=self.line_terminator)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
        return self.place(filename, buffer.getvalue(), atomic=atomic)

    def listing(self) -> list[str]:
        """Published file names, sorted, excluding partial writes.

        A consumer would see the partial files too -- they are really
        there -- but they are not published, and a silo reporting its
        own contents should report what it has published.
        """
        if not self.is_reachable():
            return []
        return sorted(
            entry.name for entry in self.path.iterdir()
            if entry.is_file() and not entry.name.endswith(PARTIAL_SUFFIX)
        )


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): place() is atomic by default via write-to-
# partial-then-rename. Writing in place lets a polling consumer read a
# half-written file, which parses as a short CSV with a mangled last row rather
# than as an error -- the worst kind of failure. atomic=False exists so that
# condition can be produced deliberately, because plenty of real publishers do
# write in place; it is a property of the upstream being simulated, not a test
# hook.
#
# RESOLVED: the CSV dialect defaults to CRLF plus a UTF-8 BOM rather than clean
# UTF-8 with newlines. Nearly all of these files are produced by or for Excel
# on Windows, which misreads UTF-8 without a BOM, so exporters emit one and
# consumers find three stray bytes on the first field name. Defaulting to the
# tidy form would be simulating a file nobody sends.
#
# RESOLVED: write_csv passes lineterminator to csv.writer and writes with
# newline="". Both are needed on Windows, where leaving translation on turns
# each "\n" into os.linesep -- already CRLF -- and yields CRLFCRLF.
#
# Untestable on linux, and said plainly rather than covered by a test that
# cannot fail: os.linesep is "\n" here, so CRLF survives translation unchanged
# and removing newline="" changes nothing. Measured directly. The assertion in
# tests/test_filedrop_silo.py is kept for what it documents; a negative control
# confirmed it does not fire. Anyone running the suite on Windows would be
# giving it real teeth for the first time.
#
# DEFERRED (known, intentional, not yet built): no inbox/archive convention.
# Real SFTP drops often move consumed files to an archive folder, and some use
# a `.done` marker file alongside each payload instead of an atomic rename.
# Both are worth simulating and neither is needed until a pack describes a
# publisher that works that way.
#
# DEFERRED: CSV only. Supplier price lists and payroll files are frequently
# .xlsx, and Foundry reads those too. Writing real xlsx means a dependency
# (openpyxl); worth it when a pack needs one, not before.
#
# DEFERRED: no file-size or retention limit. A pack publishing daily for a
# simulated year leaves 365 files, which is fine; one publishing per tick would
# not be. Whichever pack does that first should bring the policy with it.


