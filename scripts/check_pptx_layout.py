#!/usr/bin/env python3
"""Check the generated deck: slide count, fonts per slide, page size."""
import glob
import re
import zipfile

pptx = glob.glob("/workspace/ai-agent-container-platform-ppt/slides/output/*.pptx")[0]
z = zipfile.ZipFile(pptx)
slide_xmls = sorted(n for n in z.namelist()
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
print("slides:", len(slide_xmls))

pres = z.read("ppt/presentation.xml").decode("utf-8")
m = re.search(r'<p:sldSz[^/]*/>', pres)
print("page size:", m.group(0) if m else "?")

faces = set()
for name in slide_xmls:
    faces.update(re.findall(r'typeface="([^"]+)"', z.read(name).decode("utf-8")))
print("fonts used:", sorted(faces))
