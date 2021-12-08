#!/usr/bin/env python
'''
show all structures in the given .NET module.

relies on tabulate, which you can install like: `pip install tabulate`
'''
import io
import sys
import logging
import argparse
import binascii
import contextlib

import tabulate

import dnfile
import dnfile.base
import dnfile.enums


logger = logging.getLogger(__name__)


class Formatter:
    def __init__(self):
        self._indent = 0
        self._s = io.StringIO()

    def indent(self):
        self._indent += 1

    def dedent(self):
        self._indent -= 1

    def _write_indent(self):
        self._s.write("  " * self._indent)

    def write(self, s: str):
        self._write_indent()
        self._s.write(s)

    def writeln(self, s: str):
        self.write(s + "\n")

    def getvalue(self) -> str:
        return self._s.getvalue()

    HEX_BY_BYTE = ["%02x" % b for b in range(0x100)]
    ASCII_BY_BYTE = [(chr(b) if (b >= 0x20 and b <= 0x7F) else ".") for b in range(0x100)]

    def hexdump(self, buf: bytes, address=0):
        for chunk_offset in range(0, len(buf), 0x10):
            chunk = buf[chunk_offset:chunk_offset+0x10]

            self._write_indent()
            self._s.write("0x%08x:  " % (address + chunk_offset))

            for b in chunk:
                self._s.write(Formatter.HEX_BY_BYTE[b])
                self._s.write(" ")

            if len(chunk) < 0x10:
                self._s.write("   " * (0x10 - len(chunk)))

            self._s.write(" ")

            for b in chunk:
                self._s.write(Formatter.ASCII_BY_BYTE[b])

            if len(chunk) < 0x10:
                self._s.write(" " * (0x10 - len(chunk)))

            self._s.write("\n")

    def rows(self, rows):
        for line in tabulate.tabulate(rows, tablefmt="plain").split("\n"):
            self.writeln(line)


@contextlib.contextmanager
def indenting(formatter: Formatter):
    """
    example:

        ostream = Formatter()
        ostream.writeln("numbers:")
        with indenting(ostream):
            ostream.writeln("- 1")
            ostream.writeln("- 2")
    """
    try:
        formatter.indent()
        yield
    finally:
        formatter.dedent()


def render_pefile_struct(ostream: Formatter, struct):
    # like: [IMAGE_CLR_METADATA]
    obj = struct.dump_dict()
    ostream.writeln(obj["Structure"])

    with indenting(ostream):
        rows = []
        for keys in struct.__keys__:
            key = keys[0]
            value = obj[key]["Value"]
            if isinstance(value, int):
                value = hex(value)
            rows.append(("%s:" % (key), value))
        ostream.rows(rows)


def get_field_name(row, field):
    # map from something like `TypeName_StringIndex` to `TypeName`.
    # the former is the raw property name,
    # while the latter is the property we can access on the object.
    if field in (row._struct_strings or ()):
        fieldname = row._struct_strings[field]
    elif field in (row._struct_guids or ()):
        fieldname = row._struct_guids[field]
    elif field in (row._struct_blobs or ()):
        fieldname = row._struct_blobs[field]
    elif field in (row._struct_asis or ()):
        fieldname = row._struct_asis[field]
    elif field in (row._struct_codedindexes or ()):
        fieldname = row._struct_codedindexes[field][0]
    elif field in (row._struct_indexes or ()):
        fieldname = row._struct_indexes[field][0]
    elif field in (row._struct_flags or ()):
        fieldname = row._struct_flags[field][0]
    elif field in (row._struct_lists or ()):
        fieldname = row._struct_lists[field][0]
    else:
        # its not a special property,
        # just look for it directly only the object.
        fieldname = field

    return fieldname


def render_pe(ostream: Formatter, dn):
    # IMAGE_NET_DIRECTORY
    render_pefile_struct(ostream, dn.net.struct)

    # IMAGE_CLR_METADATA
    render_pefile_struct(ostream, dn.net.metadata.struct)

    ostream.writeln("streams:")

    with indenting(ostream):
        for stream in dn.net.metadata.streams_list:
            ostream.writeln(stream.struct.Name.decode("utf-8") + ":")

            with indenting(ostream):
                render_pefile_struct(ostream, stream.struct)

                ostream.writeln("data:")
                with indenting(ostream):
                    buf = stream.get_data_at_offset(0x0, stream.struct.Size)
                    # note: we display up to 0x40 bytes here.
                    # this is a random choice.
                    # if left unbounded, the output may be really long.
                    ostream.hexdump(buf[:0x40])

    ostream.writeln("tables:")

    with indenting(ostream):
        for table in dn.net.mdtables.tables_list:
            ostream.writeln(table.name + ":")

            with indenting(ostream):
                for i, row in enumerate(table.rows):
                    ostream.writeln("[%d]:" % (i + 1))
                    with indenting(ostream):
                        rows = []
                        for fields in row.struct.__keys__:
                            field = get_field_name(row, fields[0])
                            value = None

                            try:
                                v = getattr(row, field)
                            except AttributeError:
                                # such as Lists, see:
                                # https://github.com/malwarefrank/dnfile/blob/cc97eca757da9f4c850188eb02ea6f72ecefeea7/src/dnfile/base.py#L250-L252
                                logger.warning("not implemented: %s.%s", table.name, field)
                                value = "<TODO: not implemented in dnlib>"
                            else:
                                if isinstance(v, dnfile.base.CodedIndex):
                                    value = "ref table %s[%d]" % (v.table.name, v.row_index)
                                elif isinstance(v, dnfile.enums.ClrFlags):
                                    # will do this in a second pass
                                    continue
                                elif isinstance(v, bytes):
                                    if len(v) == 0:
                                        value = "(empty)"
                                    else:
                                        value = binascii.hexlify(v).decode("ascii")
                                elif isinstance(v, str):
                                    if len(v) == 0:
                                        value = "(empty)"
                                    else:
                                        value = v
                                elif isinstance(v, int):
                                    value = "0x%x" % (v)
                                else:
                                    value = str(v)
                            rows.append(("%s:" % (field), value))
                        ostream.rows(rows)

                        # write flags second, so that in the above we can align columns
                        for fields in row.struct.__keys__:
                            field = get_field_name(row, fields[0])

                            try:
                                v = getattr(row, field)
                            except AttributeError:
                                continue

                            if not isinstance(v, dnfile.enums.ClrFlags):
                                continue

                            if not any(map(lambda p: p[1], v)):
                                ostream.writeln("%s: (none)" % (field))
                            else:
                                ostream.writeln("%s:" % (field))
                                with indenting(ostream):
                                    for flag, is_set in v:
                                        if is_set:
                                            ostream.writeln(flag)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(description="A program.")
    parser.add_argument("input", type=str,
                        help="Path to input file")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Disable all output but errors")
    args = parser.parse_args(args=argv)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.quiet:
        logging.basicConfig(level=logging.ERROR)
        logging.getLogger().setLevel(logging.ERROR)
    else:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger().setLevel(logging.INFO)

    dn = dnfile.dnPE(args.input)
    if not hasattr(dn, "net"):
        logger.warning("not a .NET module")
        return

    ostream = Formatter()
    render_pe(ostream, dn)
    print(ostream.getvalue())

    return 0


if __name__ == "__main__":
    sys.exit(main())