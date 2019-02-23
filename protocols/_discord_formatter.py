# From https://github.com/Hackndo/discord-irc-sync
#
# Copyright (C) 2017-2018 Hackndo <hackndo@gmail.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <http://www.gnu.org/licenses/>.

import re

IRC_BOLD, IRC_ITALIC, IRC_UNDERLINE, IRC_RESET = ("\x02", "\x1d", "\x1f", "\x0f")
DSC_BOLD, DSC_ITALIC, DSC_UNDERLINE = ("**", "*", "__")

def replace_all(text, l):
    for t in l:
        text = text.replace(t[0], t[1])
    return text

def is_included(a,b):
    """
    Return 0 if a is included in b
    Return -1 if a intersects b but a not included in b and b not included in a
    Return 1 else
    """
    if a[1] >= b[1] and a[2] <= b[2]:
        return 0
    elif a[1] > b[1] and a[2] > b[2] or a[1] < b[1] and a[2] < b[2]:
        return -1
    else:
        return 1

class D2IFormatter():
    syntax = {
        'double_emphasis': {
            're': re.compile(r'(\*{2})([\s\S]+?)(\*{2})(?!\*)'),
            'irc': IRC_BOLD,
            'discord': DSC_BOLD
        },
        'emphasis': {
            're': re.compile(
                r'\b(_)((?:__|[^_])+?)(_)\b'  # _word_
                r'|'
                r'(\*)((?:\*\*|[^\*])+?)(\*)(?!\*)'  # *word*
            ),
            'irc': IRC_ITALIC,
            'discord': DSC_ITALIC
        },
        'underline': {
            're': re.compile(r'(_{2})([\s\S]+?)(_{2})(?!_)'),
            'irc': IRC_UNDERLINE,
            'discord': DSC_UNDERLINE
        }
    }

    rules = ['double_emphasis', 'emphasis', 'underline']

    @staticmethod
    def surround(text, surround):
        return surround + text + surround

    def replace_double_emphasis(self, matchobj):
        control_char = self.syntax['double_emphasis']['irc']
        return self.surround(matchobj.group(2).replace("\n", self.surround("\n", control_char)), control_char)

    def replace_emphasis(self, matchobj):
        control_char = self.syntax['emphasis']['irc']
        if matchobj.group(2):
            res = matchobj.group(2)
        else:
            res = matchobj.group(5)
        return self.surround(res.replace("\n", self.surround("\n", control_char)), control_char)

    def replace_underline(self, matchobj):
        control_char = self.syntax['underline']['irc']
        return self.surround(matchobj.group(2).replace("\n", self.surround("\n", control_char)), control_char)

    def sanitize(self, message):
        message = re.sub(r'\\([^A-Za-z0-9])', r'\1', message)
        return message

    def format(self, message):
        message = self.sanitize(message)
        # Surround formatted groups with IRC flags based on matching regex
        for rule in self.rules:
            regex = self.syntax[rule]['re']
            m = regex.search(message)
            if m is not None:
                message = regex.sub(getattr(self, 'replace_%s' % rule), message)
        return message


class I2DFormatter:
    B_FLAG, I_FLAG, U_FLAG = (0x01, 0x02, 0x04)

    symbols = {
        IRC_BOLD: B_FLAG,
        IRC_ITALIC: I_FLAG,
        IRC_UNDERLINE: U_FLAG,
        IRC_RESET: False
    }

    def sanitize(self, message):
        """
        Remove color tags, and format tags if no formatting setting
        Escape discord format tags
        """
        replacements = [('\\', '\\\\'), ('~~', '\\~\\~')]

        message = replace_all(message, replacements)

        message = re.sub(r'(\b)_([^\b])', r'\1\\_\2', message)
        message = re.sub(r'([^\b])_(\b)', r'\1\\_\2', message)

        message = re.sub(r'(\b)\*([^\b])', r'\1\\*\2', message)
        message = re.sub(r'([^\b])\*(\b)', r'\1\\*\2', message)

        return re.sub(r'\x03(?:\d{1,2}(?:,\d{1,2})?)?', '', message)


    def format(self, message):
        message = self.sanitize(message)

        """
        Create dict with all characters and their format
        """
        char_list = [(c, 0) for c in message]
        counter = 0
        while counter < len(char_list):
            char_tuple = char_list[counter]

            if char_tuple[0] in self.symbols:  # Formatting character
                del char_list[counter]
                for i in range(counter, len(char_list)):
                    if self.symbols[char_tuple[0]]:
                        char_list[i] = (char_list[i][0], char_list[i][1] ^ self.symbols[char_tuple[0]])
                    else:
                        char_list[i] = (char_list[i][0], 0)
            else:  # Common character. Goto next one
                counter += 1

        """
        Create intervals of formatting types
        """
        intervals = []
        bold_i = None
        underline_i = None
        italic_i = None
        for key, char_tuple in enumerate(char_list):
            if key == 0:
                if char_tuple[1] & self.B_FLAG:
                    bold_i = [DSC_BOLD, 0, False]
                if char_tuple[1] & self.I_FLAG:
                    italic_i = [DSC_ITALIC, 0, False]
                if char_tuple[1] & self.U_FLAG:
                    underline_i = [DSC_UNDERLINE, 0, False]
            else:
                if char_tuple[1] & self.B_FLAG ^ char_list[key - 1][1] & self.B_FLAG:
                    if bold_i is not None:
                        bold_i[2] = key
                        intervals.append(bold_i)
                        bold_i = None
                    else:
                        bold_i = [DSC_BOLD, key, False]
                if char_tuple[1] & self.I_FLAG ^ char_list[key - 1][1] & self.I_FLAG:
                    if italic_i is not None:
                        italic_i[2] = key
                        intervals.append(italic_i)
                        italic_i = None
                    else:
                        italic_i = [DSC_ITALIC, key, False]
                if char_tuple[1] & self.U_FLAG ^ char_list[key - 1][1] & self.U_FLAG:
                    if underline_i is not None:
                        underline_i[2] = key
                        intervals.append(underline_i)
                        underline_i = None
                    else:
                        underline_i = [DSC_UNDERLINE, key, False]

        """
        Close unclosed intervals
        """
        if bold_i is not None:
            bold_i[2] = len(char_list)
            intervals.append(bold_i)
        if italic_i is not None:
            italic_i[2] = len(char_list)
            intervals.append(italic_i)
        if underline_i is not None:
            underline_i[2] = len(char_list)
            intervals.append(underline_i)

        """
        Return if no formatting necessary
        """
        if intervals == []:
            return message

        """
        Order intervals (not included > included)
        """
        key = 0
        ordered_intervals = [] if len(intervals) > 1 else intervals
        while len(intervals) > 1:
            included = False
            current = intervals[key]
            for k_tested, interval in enumerate(intervals[key + 1:]):
                if is_included(current, interval) == 0:
                    included = True
                    continue
            if not included:
                ordered_intervals.append(intervals[key])
                del intervals[key]
            else:
                key = (key + 1) % len(intervals)
            if len(intervals) == 1:
                ordered_intervals.append(intervals[0])

        """
        Position the formatting elements
        """
        res = ''.join([c[0] for c in char_list])
        add = []
        for c in range(len(res) + 1):
            add.append([])
        for c in range(len(res)):
            for i in ordered_intervals[::-1]:
                if c == i[2] - 1:
                    add[c + 1].append(i[0])
            for i in ordered_intervals:
                if c == i[1]:
                    add[c].append(i[0])

        """
        Output the final string
        """
        result = ''.join(''.join(add[i]) + res[i] for i in range(len(res))) + ''.join(add[len(res)])
        return result
