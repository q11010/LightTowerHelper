import asyncio
import binascii
import copy
import logging
import sys
import csv
import unicodedata
import datetime
import danmakus_getter
import aiohttp.client_exceptions
import flask_cors
from bilibili_api import live, video, user, sync
import time
import threading
import json
import os
import re
import webbrowser

# simple clip-detection tool
# by 11010，licence GPL3
from flask import Flask, url_for, redirect

if getattr(sys, 'frozen', False):
    cp = os.path.dirname(os.path.abspath(sys.executable))
else:
    cp = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(cp, "settings.json"), encoding='utf-8') as f:
    settings = json.load(f)

# global variables
prem_stop_download = False
t_video_name = os.path.join(cp, "temp_video.m4s")
t_audio_name = os.path.join(cp, "temp_audio.m4s")
zoom_rate = 20
room_id = settings['room_id']['val']
whitelist_uid = settings['whitelist_uid']['val']
whitelist_dms = settings['whitelist_dms']['val']
loose_whitelist = settings['loose_whitelist']['val']
blacklist_uid = settings['blacklist_uid']['val']
blacklist_dms = settings['blacklist_dms']['val']
ref_time = settings['ref_time']['val']
avg_time = settings['avg_time']['val']
trigger_thresh = settings['trigger_thresh']['val']
timezone = settings['timezone']['val']
use_preview = settings['use_preview']['val']
blacklist_dms_re = []
whitelist_cid = []
blacklist_cid = []
stream_start_time = 0
stream_start_delta = 0
dms_list = []
hits = []
file_name = "e"
thresh_start = -1
room = live.LiveDanmaku(room_id)
room_gen = live.LiveRoom(room_id)
lock = False
is_listening = False
has_listened = False
cache_u = {}  # crc32 : uid
cache_u_name = {}  # uid : name
dms_loop = asyncio.new_event_loop()
debug_stop = False
debug_ext_data = False
debug_ext_data_list = []
stop_auto_pause = True

gen_bvid = None
gen_cid = None
gen_time = 0
filtered_hits = []
terminated = False


# slugify, from django
# https://github.com/django/django/blob/main/LICENSE

def slugify(value, allow_unicode=False):
    """
    https://github.com/django/django/blob/master/django/utils/text.py
    Convert to ASCII if 'allow_unicode' is False. Convert spaces or repeated
    dashes to single dashes. Remove characters that aren't alphanumerics,
    underscores, or hyphens. Convert to lowercase. Also strip leading and
    trailing whitespace, dashes, and underscores.
    """
    value = str(value)
    if allow_unicode:
        value = unicodedata.normalize('NFKC', value)
    else:
        value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value.lower())
    return re.sub(r'[-\s]+', '-', value).strip('-_')


def flask_start():
    app = Flask(__name__)
    cors = flask_cors.CORS(app)
    count = 0

    @app.route("/")
    def default():
        return redirect(url_for('pre_screen'))

    @app.route('/pre_screen', methods=['GET', 'POST'])
    def pre_screen():
        nonlocal count
        global hits
        global gen_time
        global filtered_hits
        if count == len(hits):
            return redirect('/terminate')
        else:
            gen_time = max(hits[count]['start'] - 10, 0)
            return_str = f'''
            <h3>准备播放片段{gen_time}，片段添加原因：{hits[count]['comment']}</h3>
            <p></p>
            <h5>片段内的弹幕例子：</h5>
            <p>
            '''
            for c in hits[count]['dms']:
                return_str += f"{c}<br>"
            return_str += f'''
            </p>
            <form action="{url_for("video")}">
                <input type="submit" value="查看这个片段" />
            </form>
            <form action="{url_for("save")}">
                <input type="submit" value="直接保存这个时间点" />
            </form>
            <form action="{url_for("discard")}">
                <input type="submit" value="不保存这个时间点" />
            </form>
            '''
            return return_str

    @app.route('/video', methods=['GET', 'POST'])
    def video():
        nonlocal count
        global hits
        global gen_time
        global filtered_hits
        if count == len(hits):
            return redirect('/terminate')
        else:
            gen_time = max(hits[count]['start'] - 10, 0)
            if gen_time != 0:
                vid = f"https://player.bilibili.com/player.html?bvid={gen_bvid}&cid={gen_cid}&page=1&share_source=copy_web&t={int(gen_time)}"
            else:
                vid = f"https://player.bilibili.com/player.html?bvid={gen_bvid}&cid={gen_cid}&page=1&share_source=copy_web"
            return f'''
            <p>正在播放片段{str(datetime.timedelta(seconds=gen_time))}，{count + 1} / {len(hits)}，{(count + 1) / len(hits)}%</p>
            <iframe src="{vid}" scrolling="no" border="0" frameborder="no" framespacing="0" allowfullscreen="true" height=480 width=852></iframe>
            <form action="{url_for("save")}">
                <input type="submit" value="保存这个时间点" />
            </form>
            <form action="{url_for("discard")}">
                <input type="submit" value="丢掉这个时间点" />
            </form>
            <form>
                <input type="submit" value="重新播放这个片段" />
            </form>
            '''

    @app.route('/terminate')
    def terminate():
        global terminated
        terminated = True
        return f'''
        <h1>所有片段已经播放完毕，过滤过的数据覆盖了原本的输出文件，可以关闭这个页面了。。。</h1>
        '''

    @app.route('/save')
    def save():
        nonlocal count
        count += 1
        filtered_hits.append(hits[count])
        return redirect(url_for('pre_screen'))

    @app.route('/discard')
    def discard():
        nonlocal count
        count += 1
        return redirect(url_for('pre_screen'))

    app.run()


def get_data():
    global room
    hit_dms = []
    global debug_ext_data_list

    @room.on('DANMU_MSG')
    async def on_danmaku(event):
        global hits
        global is_listening
        global dms_list
        global blacklist_dms
        global blacklist_uid
        global whitelist_dms
        global whitelist_uid
        global thresh_start
        global lock
        nonlocal hit_dms

        while lock:
            pass
        lock = True

        dm_time = time.time() - stream_start_time
        info = event['data']['info']
        logging.info(f"[弹幕获取器]{info[2][1]}：{info[1]}")

        for r in blacklist_dms_re:
            if r.search(info[1]):
                logging.info(f"[弹幕获取器]黑名单弹幕，跳过")
                lock = False
                return
        if info[2][0] in blacklist_uid:
            logging.info(f"[弹幕获取器]黑名单账号，跳过")
            lock = False
            return
        if info[1] in whitelist_dms and (info[2][0] in whitelist_uid or loose_whitelist):
            logging.info(f"[弹幕获取器]添加手动路灯")
            hits.append({"start": dm_time, "end": dm_time, "comment": f"手动：uid {info[2][0]}, 用户名 {info[2][1]}",
                         "dms": [info[1]]})
        dms_list.append({"time": dm_time, "word": info[1], "uid": info[2][0], "name": info[2][1]})
        while True:
            if dm_time - dms_list[0]['time'] > ref_time:
                del dms_list[0]
            else:
                break
        hit_list = []
        for val in dms_list:
            if dm_time - val['time'] <= avg_time:
                hit_list.append(val)

        local_ref_time = min(ref_time, int(dm_time - stream_start_delta))
        local_ref_time = max(avg_time, local_ref_time)
        avg = len(hit_list) / avg_time
        ref = len(dms_list) / local_ref_time
        if debug_ext_data:
            debug_ext_data_list.append([dm_time, avg, ref, avg / ref, trigger_thresh])
        logging.info(f"[弹幕获取器]{avg_time}秒平均弹幕：{avg}，{local_ref_time}秒平均弹幕：{ref}，比例：{avg / ref}")
        if ref == 0:
            logging.info(f"[弹幕获取器]基础平均值 = 0，跳过")
        if avg / ref > trigger_thresh and thresh_start == -1:
            logging.info(f"[弹幕获取器]比例第一次高于{trigger_thresh}，记录开始时间")
            thresh_start = dm_time
            hit_dms = copy.deepcopy(hit_list)
        elif avg / ref > trigger_thresh:
            hit_dms.append({"time": dm_time, "word": info[1], "uid": info[2][0], "name": info[2][1]})
        if avg / ref <= trigger_thresh and thresh_start != -1:
            logging.info(f"[弹幕获取器]比例第一次低于{thresh_start}，记录结束时间")
            hits.append({"start": thresh_start, "end": dm_time, "comment": "自动添加", "dms": [d['word'] for d in hit_dms]})
            hit_dms = []
            thresh_start = -1
        lock = False

    logging.info(f"[弹幕获取器]启动成功")
    dms_loop.run_until_complete(room.connect())


def is_alive():
    global stream_start_time
    global file_name
    global is_listening
    global stream_start_delta
    global lock
    global debug_stop
    while True:
        logging.info(f"[直播状态监听器]获取直播状态ing。。。")
        while True:
            try:
                info = sync(room_gen.get_room_info())['room_info']
                break
            except aiohttp.client_exceptions.ServerDisconnectedError:
                time.sleep(1)
                logging.info(f"[直播状态监听器]服务器断开，重试中。。。")
        logging.info(f"[直播状态监听器]获取成功，直播状态：{info['live_status']}，开始时间：{info['live_start_time']}，标题：{info['title']}")
        if (info['live_status'] != 1 and is_listening) or debug_stop:
            logging.info(f"[直播状态监听器]开始关闭直播间")
            try:
                sync(room.disconnect())
            except Exception as e:
                logging.info(e)
            is_listening = False
            debug_stop = False
            hits.append({"start": thresh_start, "end": -1, "comment": "自动添加，到直播结束", 'dms': None})
            lock = False
            logging.info(f"[直播状态监听器]直播间关闭完毕")
        if info['live_status'] == 1 and stream_start_time == 0:
            stream_start_time = info['live_start_time']
            stream_start_delta = time.time() - info['live_start_time']
            file_name = str(room_id) + "_" + info['title'] + "_" \
                        + time.strftime("%Y-%m-%d_%H:%M:%S", time.localtime(info['live_start_time'] + timezone * 3600))
            file_name = slugify(file_name, allow_unicode=True) + ".json"
        time.sleep(30)


def xml_get_dms(bvid):
    global file_name
    global gen_bvid
    global gen_cid

    logging.info(f"[录播弹幕下载器]开始下载录播{bvid}")
    v = video.Video(bvid=bvid)
    info = sync(v.get_info())
    cid = info['cid']
    file_name = str(room_id) + "_" + info['title'] + "_" \
                + time.strftime("%Y-%m-%d_%H:%M:%S",
                                time.localtime(time.time() + timezone * 3600))
    file_name = file_name.replace(' ', "_")
    file_name = slugify(file_name, allow_unicode=True) + "_unalive.json"
    logging.info(f"[录播弹幕下载器]获取录播信息成功，cid={cid}")
    gen_bvid = bvid
    gen_cid = cid
    # xml download, loose info
    # default_headers = {
    #     "Referer": "https://www.bilibili.com",
    #     "User-Agent": "Mozilla/5.0"
    # }
    # r = requests.get(
    #     f"http://comment.bilibili.com/{cid}.xml",
    #     headers=default_headers)
    # if r.status_code == 412:
    #     logging.info(f"[录播弹幕下载器]412错误")
    #     sys.exit(0)
    # if r.status_code != 200:
    #     raise ConnectionError(r.status_code)
    # r.encoding = r.apparent_encoding
    # root = ET.fromstring(r.text)
    # for child in root:
    #     if child.tag == 'd':
    #         ps = child.attrib['p'].split(',')
    #         dm = Danmaku(text=child.text,
    #                     dm_time=float(ps[0]),
    #                     crc32_id=ps[6])
    #         dms.append(dm)
    # recursive download, no loss
    dms = danmakus_getter.get_dms(bvid)
    logging.info(f"[录播弹幕下载器]弹幕文件下载成功")
    out = []
    while len(dms) > 0:
        min_dm = None
        for dm in dms:
            if min_dm is None:
                min_dm = dm
            elif dm.dm_time < min_dm.dm_time:
                min_dm = dm
        out.append(min_dm)
        dms.remove(min_dm)
    logging.info(f"[录播弹幕下载器]弹幕文件整理成功")
    return out


def get_static_data(bvid: str):
    global hits
    global dms_list
    global blacklist_dms
    global blacklist_uid
    global whitelist_dms
    global whitelist_uid
    global thresh_start
    global cache_u
    global cache_u_name
    global stream_start_delta

    dms = xml_get_dms(bvid)
    hit_dms = []
    for dm in dms:
        dm_time = dm.dm_time
        u = dm.crc32_id
        text = dm.text
        logging.info(f"[弹幕获取器]{u}：{text}, {dm_time}")
        gen_cont = False
        for r in blacklist_dms_re:
            if r.search(text):
                logging.info(f"[弹幕获取器]黑名单弹幕，跳过")
                gen_cont = True
                break
        if gen_cont:
            continue
        if u in blacklist_cid:
            logging.info(f"[弹幕获取器]黑名单账号，跳过")
            continue
        if text in whitelist_dms and (u in whitelist_cid or loose_whitelist):
            logging.info(f"[弹幕获取器]添加手动路灯")
            if str(u) in cache_u_name:
                u_name = cache_u_name[str(u)]
            else:
                logging.info(f"[弹幕获取器]获取用户名：{u}")
                us = user.User(uid=dm.crack_uid())
                try:
                    u_name = sync(us.get_user_info())['name']
                except Exception as e:
                    print(e)
                    u_name = us.uid
                cache_u_name[str(u)] = u_name
                logging.info(f"[弹幕获取器]用户名：{u_name}")
            hits.append(
                {"start": dm_time, "end": dm_time, "comment": f"手动：uid {u}, 用户名 {u_name}", 'dms': [text]})
        dms_list.append({"time": dm_time, "word": text, "uid": u, "name": None})
        while True:
            if dm_time - dms_list[0]['time'] > ref_time:
                del dms_list[0]
            else:
                break
        hit_list = []
        for val in dms_list:
            if dm_time - val['time'] <= avg_time:
                hit_list.append(val)

        local_ref_time = min(ref_time, int(dm_time - stream_start_delta))
        local_ref_time = max(avg_time, local_ref_time)
        avg = len(hit_list) / avg_time
        ref = len(dms_list) / local_ref_time
        debug_ext_data_list.append([dm_time, avg, ref, avg / ref, trigger_thresh])
        logging.info(f"[弹幕获取器]{avg_time}秒平均弹幕：{avg}，{local_ref_time}秒平均弹幕：{ref}，比例：{avg / ref}")
        if ref == 0:
            logging.info(f"[弹幕获取器]基础平均值 = 0，跳过")
        if avg / ref > trigger_thresh and thresh_start == -1:
            logging.info(f"[弹幕获取器]比例第一次高于{trigger_thresh}，记录开始时间")
            hit_dms = copy.deepcopy(hit_list)
            thresh_start = dm_time
        elif avg / ref > trigger_thresh:
            hit_dms.append({"time": dm_time, "word": text, "uid": u, "name": None})
        if avg / ref <= trigger_thresh and thresh_start != -1:
            logging.info(f"[弹幕获取器]比例第一次低于{thresh_start}，记录结束时间")
            hits.append(
                {"start": thresh_start, "end": dm_time, "comment": "自动添加", "dms": [d['word'] for d in hit_dms]})
            thresh_start = -1


def build_cids():
    logging.info(f"[cid翻译]翻译黑名单/白名单uid")
    global whitelist_uid
    global whitelist_cid
    global blacklist_uid
    global blacklist_cid
    for u in blacklist_uid:
        blacklist_cid.append(str(hex(binascii.crc32(bytes(str(u), 'utf-8')) & 0xffffffff)).split('x', 1)[1])
    for u in whitelist_uid:
        whitelist_cid.append(str(hex(binascii.crc32(bytes(str(u), 'utf-8')) & 0xffffffff)).split('x', 1)[1])


if __name__ == "__main__":
    f = "%(asctime)s: %(message)s"
    if "--log" in sys.argv:
        logging.basicConfig(format=f, level=logging.INFO,
                            datefmt="%H:%M:%S")
    else:
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
    for bl in blacklist_dms:
        blacklist_dms_re.append(re.compile(bl))

    debug_ext_data = "--debug" in sys.argv

    if "--unalive" in sys.argv:
        bv = input("\n=========\n请输入录播bvid\n=========\n>> ")
        vd = video.Video(bv)
        print("\n=========\n获取数据中，请稍后。。。\n这个过程可能需要几分钟\n=========\n")
        build_cids()
        get_static_data(bv)
        logging.info(f"[主程序]本场录播的已经触发的时间点：{[h['start'] for h in hits]}")
        with open(os.path.join(cp, file_name), 'w', encoding='utf-8') as f:
            json.dump(hits, f, ensure_ascii=False, indent=4)
        if debug_ext_data:
            with open(os.path.join(cp, "debug_" + file_name + ".csv"), 'w', encoding='utf-8', newline='') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(["time", "average", "ref_average", "ratio", "thresh"])
                for item in debug_ext_data_list:
                    writer.writerow(item)
        print(f"\n=========\n时间点保存完毕，文件名：{file_name}\n=========\n")
        if use_preview:
            current_time = 0
            while True:
                time.sleep(1)
                use_preview_input = input(f"\n=========\n是否预览时间点对应的片段？(y/n)\n=========\n>> ")
                if use_preview_input == 'n':
                    sys.exit(0)
                elif use_preview_input == 'y':
                    flask_thread = threading.Thread(target=flask_start, daemon=True)
                    flask_thread.start()
                    time.sleep(1)
                    try:
                        webbrowser.open("http://127.0.0.1:5000/")
                    except:
                        print(f"\n=========\n注意，预览网站肯能没自动打开，请手动打开下面的网页\n=========\n")
                    print(f"\n=========\n在新页面内开启了播放器\n全播放完后这个窗口会自动关闭\n"
                          f"如果不小心关闭了浏览器，可以重新打开网站：\nhttp://127.0.0.1:5000/\n=========\n")
                    while not terminated:
                        time.sleep(3)
                    print(f"\n=========\n时间点保存完毕，文件名：{file_name}\n=========\n")
                    with open(os.path.join(cp, file_name), 'w', encoding='utf-8') as f:
                        json.dump(filtered_hits, f, ensure_ascii=False, indent=4)
                    sys.exit(0)
        sys.exit(0)

    if "--alive" in sys.argv:
        alive_daemon = threading.Thread(target=is_alive, daemon=True)
        alive_daemon.start()
        time.sleep(2)
        while True:
            if not is_listening and not has_listened and stream_start_time != 0:
                logging.info(
                    f"[主程序]启动弹幕获取器")
                is_listening = True
                has_listened = True
                listener = threading.Thread(target=get_data,
                                            daemon=True)
                listener.start()
            if not is_listening and has_listened:
                logging.info(
                    f"[主程序]记录上场直播的可能时间点")
                with open(os.path.join(cp, file_name), 'w', encoding='utf-8') as f:
                    json.dump(hits, f, ensure_ascii=False, indent=4)
                if debug_ext_data:
                    with open(os.path.join(cp, "debug_" + file_name + ".csv"), 'w', encoding='utf-8',
                              newline='') as csvfile:
                        writer = csv.writer(csvfile)
                        writer.writerow(["time", "average", "ref_average", "ratio", "thresh"])
                        for item in debug_ext_data_list:
                            writer.writerow(item)
                    debug_ext_data_list = []
                stream_start_time = 0
                stream_start_delta = 0
                hits = []
                file_name = ""
                has_listened = False
            if is_listening:
                logging.info(f"[主程序]本场直播的已经触发的时间点：{[h['start'] for h in hits]}")
            time.sleep(30)

    print("未指定模式！请指定是查直播还是查录播！")
    print("--alive: 直播")
    print("--unalive: 录播")
