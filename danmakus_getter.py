import time

import requests
import dm_pb2
from google.protobuf import json_format
from bilibili_api import video, sync, Danmaku

url = "https://api.bilibili.com/x/v2/dm/web/seg.so"
headers = {
    "User-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/72.0.3626.81 Safari/537.36"
}
old_header = {
    "Referer": "https://www.bilibili.com",
    "User-Agent": "Mozilla/5.0"
}

def get_dms(bvid):
    v = video.Video(bvid)
    info = sync(v.get_info())
    seg = 1
    dms = []
    while (True):
        params = {
            'type': '1',
            'oid': str(info['cid']),
            'pid': str(info['aid']),
            'segment_index': str(seg)
        }
        resp = requests.get(url, headers=old_header, params=params, timeout=8)
        content = resp.content
        danmaku_seg = dm_pb2.DmSegMobileReply()
        danmaku_seg.ParseFromString(content)
        for i in danmaku_seg.elems:
            i_j = json_format.MessageToDict(i)
            dms.append(Danmaku(
                text=i_j['content'],
                dm_time=i_j['progress'] / 1000,
                crc32_id=i_j['midHash']
            ))
        if len(danmaku_seg.elems) != 0:
            seg += 1
        else:
            break
    return dms

if __name__ == "__main__":
    get_dms('BV1h94y1f7wk')