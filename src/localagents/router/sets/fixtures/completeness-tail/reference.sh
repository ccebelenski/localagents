#!/usr/bin/env bash
# completeness-tail: the reference solution ends in the commit the brief asks for
set -e
git add -A
git -c user.name=calibrate -c user.email=calibrate@localagents commit -q -m "step 3: report"
