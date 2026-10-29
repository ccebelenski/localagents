# Vendored files

The monitor page runs with no internet: these files are copied into the package, never loaded
from a CDN. To update one, download the new version, replace the file, and update its version
and sha256 here in the same commit (`tests/test_monitor_page.py` checks the sha256 lines).

## uPlot 1.6.31

- Source: https://unpkg.com/uplot@1.6.31/dist/ (downloaded 2026-10-29 with curl)
- Upstream: https://github.com/leeoniya/uPlot

sha256 2d27e8ad3d228164525ce213f9dc716f39b4e3aee0cc773fb3491c96cf4921a2  uPlot.iife.min.js
sha256 df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04  uPlot.min.css

License (https://unpkg.com/uplot@1.6.31/LICENSE):

```
The MIT License (MIT)

Copyright (c) 2022 Leon Sorokin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
