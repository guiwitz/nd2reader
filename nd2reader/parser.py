# -*- coding: utf-8 -*-

import array
from collections import namedtuple
import struct
from StringIO import StringIO

field_of_view = namedtuple('FOV', ['number', 'x', 'y', 'z', 'pfs_offset'])


class Nd2Parser(object):
    CHUNK_HEADER = 0xabeceda
    CHUNK_MAP_START = "ND2 FILEMAP SIGNATURE NAME 0001!"
    CHUNK_MAP_END = "ND2 CHUNK MAP SIGNATURE 0000001!"
    
    """
    Reads .nd2 files, provides an interface to the metadata, and generates numpy arrays from the image data.

    """
    def __init__(self, filename):
        self._absolute_start = None
        self._filename = filename
        self._fh = None
        self._chunk_map_start_location = None
        self._cursor_position = None
        self._dimension_text = None
        self._label_map = {}
        self.metadata = {}
        self._read_map()
        self._parse_metadata()

    @property
    def _file_handle(self):
        if self._fh is None:
            self._fh = open(self._filename, "rb")
        return self._fh

    @property
    def _dimensions(self):
        if self._dimension_text is None:
            for line in self.metadata['ImageTextInfo']['SLxImageTextInfo'].values():
                if "Dimensions:" in line:
                    metadata = line
                    break
            else:
                raise ValueError("Could not parse metadata dimensions!")
            for line in metadata.split("\r\n"):
                if line.startswith("Dimensions:"):
                    self._dimension_text = line
                    break
            else:
                raise ValueError("Could not parse metadata dimensions!")
        return self._dimension_text

    @property
    def _image_count(self):
        return self.metadata['ImageAttributes']['SLxImageAttributes']['uiSequenceCount']

    @property
    def _sequence_count(self):
        return self.metadata['ImageEvents']['RLxExperimentRecord']['uiCount']

    def _parse_metadata(self):
        for label in self._label_map.keys():
            if not label.endswith("LV!") or "LV|" in label:
                continue
            data = self._read_chunk(self._label_map[label])
            stop = label.index("LV")
            self.metadata[label[:stop]] = self._read_metadata(data, 1)

    def _read_map(self):
        """
        Every label ends with an exclamation point, however, we can't directly search for those to find all the labels
        as some of the bytes contain the value 33, which is the ASCII code for "!". So we iteratively find each label,
        grab the subsequent data (always 16 bytes long), advance to the next label and repeat.

        """
        self._file_handle.seek(-8, 2)
        chunk_map_start_location = struct.unpack("Q", self._file_handle.read(8))[0]
        self._file_handle.seek(chunk_map_start_location)
        raw_text = self._file_handle.read(-1)
        label_start = raw_text.index(Nd2Parser.CHUNK_MAP_START) + 32

        while True:
            data_start = raw_text.index("!", label_start) + 1
            key = raw_text[label_start: data_start]
            location, length = struct.unpack("QQ", raw_text[data_start: data_start + 16])
            if key == Nd2Parser.CHUNK_MAP_END:
                # We've reached the end of the chunk map
                break
            self._label_map[key] = location
            label_start = data_start + 16

    def _read_chunk(self, chunk_location):
        """
        Gets the data for a given chunk pointer

        """
        self._file_handle.seek(chunk_location)
        # The chunk metadata is always 16 bytes long
        chunk_metadata = self._file_handle.read(16)
        header, relative_offset, data_length = struct.unpack("IIQ", chunk_metadata)
        if header != Nd2Parser.CHUNK_HEADER:
            raise ValueError("The ND2 file seems to be corrupted.")
        # We start at the location of the chunk metadata, skip over the metadata, and then proceed to the
        # start of the actual data field, which is at some arbitrary place after the metadata.
        self._file_handle.seek(chunk_location + 16 + relative_offset)
        return self._file_handle.read(data_length)

    def _z_level_count(self):
        st = self._read_chunk(self._label_map["CustomData|Z!"])
        return len(array.array("d", st))

    def _parse_unsigned_char(self, data):
        return struct.unpack("B", data.read(1))[0]

    def _parse_unsigned_int(self, data):
        return struct.unpack("I", data.read(4))[0]

    def _parse_unsigned_long(self, data):
        return struct.unpack("Q", data.read(8))[0]

    def _parse_double(self, data):
        return struct.unpack("d", data.read(8))[0]

    def _parse_string(self, data):
        value = data.read(2)
        while not value.endswith("\x00\x00"):
            # the string ends at the first instance of \x00\x00
            value += data.read(2)
        return value.decode("utf16")[:-1].encode("utf8")

    def _parse_char_array(self, data):
        array_length = struct.unpack("Q", data.read(8))[0]
        return array.array("B", data.read(array_length))

    def _parse_metadata_item(self, args):
        data, cursor_position = args
        new_count, length = struct.unpack("<IQ", data.read(12))
        length -= data.tell() - cursor_position
        next_data_length = data.read(length)
        value = self._read_metadata(next_data_length, new_count)
        # Skip some offsets
        data.read(new_count * 8)
        return value

    def _get_value(self, data, data_type):
        parser = {1: {'method': self._parse_unsigned_char, 'args': data},
                  2: {'method': self._parse_unsigned_int, 'args': data},
                  3: {'method': self._parse_unsigned_int, 'args': data},
                  5: {'method': self._parse_unsigned_long, 'args': data},
                  6: {'method': self._parse_double, 'args': data},
                  8: {'method': self._parse_string, 'args': data},
                  9: {'method': self._parse_char_array, 'args': data},
                  11: {'method': self._parse_metadata_item, 'args': (data, self._cursor_position)}}
        return parser[data_type]['method'](parser[data_type]['args'])

    def _read_metadata(self, data, count):
        data = StringIO(data)
        metadata = {}
        for _ in xrange(count):
            self._cursor_position = data.tell()
            header = data.read(2)
            if not header:
                break
            data_type, name_length = map(ord, header)
            name = data.read(name_length * 2).decode("utf16")[:-1].encode("utf8")
            value = self._get_value(data, data_type)
            if name not in metadata:
                metadata[name] = value
            else:
                if not isinstance(metadata[name], list):
                    # We have encountered this key exactly once before. Since we're seeing it again, we know we
                    # need to convert it to a list before proceeding.
                    metadata[name] = [metadata[name]]
                # We've encountered this key before so we're guaranteed to be dealing with a list. Thus we append
                # the value to the already-existing list.
                metadata[name].append(value)
        return metadata