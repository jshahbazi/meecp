"""Synthetic owned-build acceptance; emit counts and resource timing only."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import resource
import time

import numpy as np
from PIL import Image

from detector import DetectorError, HotdogDetector, MAX_BYTES, MAX_PIXELS, inline_image, postprocess


def verify():
    detector = HotdogDetector()
    detector.load()
    fixtures = json.loads((Path(__file__).parent / "fixtures.json").read_bytes())
    for fixture in fixtures:
        assert detector.detect(inline_image(fixture["arguments"]["image_base64"])) == fixture["expected"]
    output = np.zeros((1,84,8400),dtype=np.float32)
    for index,(x,y,w,h,kind,score) in enumerate([(150,125,100,60,52,.9),(155,127,100,60,52,.8),
                                               (250,125,40,40,52,.7),(160,100,40,40,1,.99)]):
        output[0,:4,index] = [x,y,w,h]; output[0,4+kind,index] = score
    assert postprocess(output,.35,.5,100,50,400,300) == [
        {"confidence":.9,"box":[0.,90.,200.,210.]},{"confidence":.7,"box":[260.,110.,340.,190.]}]
    rejected = 0
    for mode in ("shape","nan","inf","dtype","score"):
        wrong = np.zeros((1,84,8400),dtype=np.float32)
        if mode == "shape": wrong = wrong[:,:,:1]
        if mode == "nan": wrong[0,4,0] = np.nan
        if mode == "inf": wrong[0,4,0] = np.inf
        if mode == "dtype": wrong = wrong.astype(np.float64)
        if mode == "score": wrong[0,56,0] = 1.01
        try:
            postprocess(wrong,.35,1.,0,0,640,640)
        except DetectorError:
            rejected += 1
    assert rejected == 5
    peak_seconds, cases = 0., 0
    # The byte/pixel limits are independent: compressed maximum-area images,
    # palette/16-bit/RGBA decoding, and an exact maximum-size encoded request.
    for size,mode,kind,padded in [((1024,1024),"RGB","PNG",False),((4096,256),"RGB","PNG",False),
        ((256,4096),"RGB","PNG",False),((1024,1024),"RGBA","PNG",False),
        ((1024,1024),"I;16","PNG",False),((1024,1024),"P","GIF",False),
        ((1024,1024),"RGB","PNG",True)]:
        image = Image.new(mode,size); buffer = io.BytesIO(); image.save(buffer,format=kind); image.close()
        data = buffer.getvalue()
        if padded:
            data += b"\0" * (MAX_BYTES - len(data))
            assert len(data) == MAX_BYTES
        assert 0 < len(data) <= MAX_BYTES and size[0]*size[1] == MAX_PIXELS
        started = time.monotonic()
        result = detector.detect(inline_image(base64.b64encode(data).decode()))
        elapsed = time.monotonic() - started
        assert result["image"] == {"width":size[0],"height":size[1]}
        assert elapsed < 60
        peak_seconds = max(peak_seconds, elapsed); cases += 1
    for size in [(1025,1024),(4097,1)]:
        image = Image.new("RGB",size); buffer=io.BytesIO(); image.save(buffer,format="PNG"); image.close()
        try:
            detector.detect(buffer.getvalue())
        except DetectorError as error:
            assert str(error) == "image_pixel_limit"
        else:
            raise AssertionError("pixel_boundary_accepted")
    try:
        inline_image(base64.b64encode(b"\0"*(MAX_BYTES+1)).decode())
    except DetectorError as error:
        assert str(error) == "image_size_limit"
    else:
        raise AssertionError("byte_boundary_accepted")
    return {"schema_version":1,"model_valid":True,"blank_images":len(fixtures),"controlled_tensor_cases":6,
        "max_image_cases":cases,"max_input_bytes":MAX_BYTES,"max_pixels":MAX_PIXELS,
        "max_inference_seconds":round(peak_seconds,6),"boundary_rejections":3,
        "peak_rss_bytes":int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)}
