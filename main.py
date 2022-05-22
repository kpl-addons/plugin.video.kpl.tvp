
from libka import L, Plugin, Site
from libka import call, PathArg, entry
from libka.logs import log
from libka.url import URL
from libka.path import Path
from libka.menu import Menu, MenuItems
from libka.utils import html_json, html_json_iter
from libka.format import safefmt
from libka.lang import day_label, text as lang_text
from libka.calendar import str2date
from libka.search import search
# from pdom import select as dom_select
import json
from collections.abc import Mapping
from collections import namedtuple
from html import unescape
from datetime import datetime, timedelta
import re
from enum import IntEnum
import requests  # onlu for status code ok
import xbmcgui  # dialogs
import xbmcplugin  # setResolvedUrl
import xbmcvfs  # for file in m3u generator
try:
    from ttml2ssa import Ttml2SsaAddon
except ModuleNotFoundError:
    Ttml2SsaAddon = None  # DEBUG only


# XXX
# Na razie wszystko jest w jednym pliku, bo łatwiej odświeżać w kodi.
# Potem poszczególne klasy wylądują w resources/lib/
# XXX

# Some "const" defines (as unique values).
UNLIMITED = object()
Future = object()
CurrentAndFuture = object()


class TransmissionLayout(IntEnum):
    DayFolder = 0
    DayLabel = 1
    SingleList = 2


def remove_tags(text):
    def sub(match):
        tag = remove_tags.replace.get(match.group('tag'))
        if tag:
            close = match.group('close') or ''
            return f'[{close}{tag}]'
        return ''

    if not text:
        return text
    return remove_tags.re_tags.sub(sub, text)


remove_tags.re_tags = re.compile(r'<(?P<close>/)?(?P<tag>\w+)\b.*?>', re.DOTALL)
remove_tags.replace = {
    'br': 'CR',
    'b': 'B',
    'strong': 'B',
    'i': 'I',
    'em': 'I',
}


def linkid(url):
    """Returns ID from TVP link."""
    return url.rpartition(',')[2]


def image_link(image):
    """Return URL to image by its JSON item."""
    if image is None:
        return None
    width = image.get('width', 1280)
    height = image.get('height', 720)
    if 'url' in image:
        url = image['url']
        return URL(url.format(width=width, height=height))
    if 'file_name' not in image:
        return None
    fname: str = image['file_name']
    name, _, ext = fname.rpartition('.')
    return URL(f'http://s.v3.tvp.pl/images/{name[:1]}/{name[1:2]}/{name[2:3]}/uid_{name}_width_{width}_gs_0.{ext}')


StreamType = namedtuple('StreamType', 'proto mime')
Stream = namedtuple('Stream', 'url proto mime')
EuVideo = namedtuple('EuVideo', 'width height rate url')

ChannelInfo = namedtuple('ChannelInfo', 'code name image id')



class Info(namedtuple('Info', 'data type url title image descr series linkid')):

    @classmethod
    def parse(cls, data):
        try:
            data = json.loads(unescape(data))
            eLink = data.get('episodeLink')
            sLink = data.get('seriesLink')
            url = URL(sLink)
            # 'episodeCount'
            # TODO:  dodać analizę w zlaezności od typu i różnic w obu linkach
            #        np. "video" i takie same linki wskazuję bezpośrednio film
            image = data['image']
            return Info(data, type=data['type'], url=url, title=data['title'], image=image,
                        descr=data.get('description'), series=(eLink != sLink),
                        linkid=url.path.rpartition(',')[2])
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            log.warning(f'Can not parse video info {exc} from: {data!r}')
            return None


class TvpVodSite(Site):
    """vod.tvp.pl site."""


class TvpSite(Site):
    """TVP API."""

    def __init__(self, base='https://www.api.v3.tvp.pl/', *args, count=None, verify_ssl=False, **kwargs):
        super().__init__(base, *args, verify_ssl=verify_ssl, **kwargs)
        self.count = count
        self.dT = timedelta(minutes=5)  # time epsilon (extrnd filter time range)

    def listing(self, parent_id, *, dump='json', direct=True, **kwargs):
        count = kwargs.pop('count', self.count)
        if count is None or count is UNLIMITED:
            count = ''
        if kwargs.get('page', ...) is None:
            kwargs.pop('page')
        return self.jget('/shared/listing.php',
                         params={'dump': dump, 'direct': direct, 'count': count, 'parent_id': parent_id, **kwargs})

    def listing_items(self, parent_id, *, dump='json', direct=True, **kwargs):
        data = self.listing(parent_id, dump=dump, direct=direct, **kwargs)
        for item in data.get('items') or ():
            yield item

    # Dicts `filter` and `order` could be in arguments because they are read-only.
    def transmissions(self, parent_id, *, dump='json', direct=False, type='epg_item',
                      filter={'is_live': True}, order={'release_date_long': -1}, **kwargs):
        if filter is CurrentAndFuture:
            filter = f'broadcast_end_date_long>={(datetime.now() - self.dT).timestamp()*1000}'
        elif filter is Future:
            filter = f'release_date_long>={(datetime.now() - self.dT).timestamp()*1000}'
        return self.listing(parent_id, dump=dump, direct=direct, type=type, filter=filter, order=order, **kwargs)

    # Dicts `filter` and `order` could be in arguments because they are read-only.
    def transmissions_items(self, parent_id, *, dump='json', direct=False, type='epg_item',
                            filter={'is_live': True}, order={'release_date_long': -1}, **kwargs):
        data = self.transmissions(parent_id, dump=dump, direct=direct, type=type, filter=filter, order=order, **kwargs)
        # reverse reversed ('release_date_long': -1) list
        return reversed(data.get('items') or ())

    def details(self, object_id, *, dump='json', **kwargs):
        return self.jget('/shared/details.php',
                         params={'dump': dump, 'object_id': object_id, **kwargs})

    def stations(self):
        return self.jget('https://tvpstream.tvp.pl/api/tvp-stream/program-tv/stations').get('data') or ()

    def station_epg(self, station_code, date):
        return self.jget('https://tvpstream.tvp.pl/api/tvp-stream/program-tv/index', params={
            'station_code': station_code,
            'date': date,
        }).get('data') or ()

    def station_program(self, station_code, record_id):
        return self.jget('https://tvpstream.tvp.pl/api/tvp-stream/stream/data', params={
            'station_code': station_code,
            'record_id': record_id,
        })

    def station_streams(self, station_code, record_id):
        data = self.station_program(station_code=station_code, record_id=record_id)
        url = data.get('data', {}).get('stream_url')
        return self.jget(url).get('formats')

    def blackburst(self, parent_id, *, dump='json', direct=False, type='video', nocount=1, copy=False,
                   filter={'playable': True}, order='release_date_long,-1', release_date=None, **kwargs):
        count = kwargs.pop('count', self.count)
        if count is None or count is UNLIMITED:
            count = ''
        if kwargs.get('page', ...) is None:
            kwargs.pop('page')
        filter = dict(filter)
        if release_date:
            filter['release_date_long'] = {'$lt': release_date.timestamp() * 1000}
        # filter['play_mode'] = 1
        return self.jget('/shared/listing_blackburst.php',
                         params={'dump': dump, 'direct': direct, 'count': count, 'parent_id': parent_id,
                                 'nocount': nocount, 'copy': copy, 'type': type, 'filter': filter, 'order': order,
                                 **kwargs})


class TvpPlugin(Plugin):
    """tvp.pl plugin."""

    MENU = Menu(order_key='title', items=[
        Menu(title='Tests', when='debugging', items=[
            Menu(title=L(30104, 'API Tree'), id=2),
            Menu(title=L(30113, 'VoD'), id=1785454),
            Menu(title=L(30114, 'Rebroadcast'), id=48583081),
            Menu(title="m1992's TV", id=68970),
            Menu(call='tv_hbb'),
            Menu(call='tv_stations'),
            Menu(call='tv_html'),
            Menu(call='tv_tree'),
        ]),
        Menu(title=L(30105, 'TV'), items=[
            Menu(call='tv'),
            Menu(call='replay_list'),
        ]),
        MenuItems(id=1785454, type='directory_series', order={2: 'programy', 1: 'seriale', -1: 'teatr*'}),
        # Menu(title='Rekonstrucja cyfrowa', id=35470692),  --- jest już powyższym w MenuItems(1785454)
        Menu(title=L(30119, 'Sport'), items=[
            Menu(title=L(30117, 'Broadcast'), call=call('transmissions', 13010508)),
            Menu(title=L(30114, 'Rebroadcast'), id=48583081),
            Menu(title=L(30120, 'Sport magazines'), id=548368),
            Menu(title=L(30121, 'Video'), id=432801),
        ]),
        Menu(title=L(30122, 'TVP Info'), id=191888),
        Menu(title=L(30116, 'Parliament'), items=[
            Menu(title=L(30117, 'Broadcast'), call=call('transmissions', 4422078)),
            Menu(title=L(30114, 'Rebroadcast'), id=4433578),
            Menu(title=L(30118, 'EuroParliament'), id=4615555),
        ]),
        Menu(title=lang_text.search, items=[
            Menu(call='search'),
        ]),
        # Menu(call='settings'),
    ])

    TYPES_ALLOWED = {
        'video',
        'directory_video',
        'website',
        'directory_series',
        'directory_toplist',
        'directory_standard',
        'directory_epg',
        'epg_item',
        'directory_stats',
        'virtual_channel',
    }

    epg_url = 'http://www.tvp.pl/shared/programtv-listing.php?station_code={code}&count=100&filter=[]&template=json%2Fprogram_tv%2Fpartial%2Foccurrences-full.html&today_from_midnight=1&date=2022-04-25'

    def __init__(self):
        super().__init__()
        self.site = TvpSite()
        self.colors['spec'] = 'gold'

    def home(self):
        self.menu()
        # with self.directory() as kdir:
        #     kdir.menu(L('Tests'), self.tests)
        #     self._menu(kdir)

    # def tests(self):
    #     with self.directory() as kdir:
    #         kdir.menu(L('API Tree'), call(self.listing, 2))
    #         kdir.menu('VoD', call(self.listing, 1785454))
    #         kdir.menu('Retransmisje', call(self.listing, 48583081))

    def enter_listing(self, id: PathArg[int]):
        # type = 0  - ShowAndGetNumber
        n = xbmcgui.Dialog().numeric(0, 'ID', str(id))
        if n:
            n = int(n)
            if n > 0:
                self.refresh(call(self.listing, n))

    def menu_entry(self, *, entry, kdir, index_path):
        if entry.id:
            return kdir.menu(entry.title, call(self.listing, entry.id))

    def menu_entry_iter(self, *, entry):
        for it in self.site.listing_items(entry.id):
            if not entry.type or it.get('object_type') == entry.type:
                yield it

    def menu_entry_item(self, *, kdir, entry, item, index_path):
        return self._item(kdir, item)

    def listing(self, id: PathArg[int], page=None, type=None):
        """Use api.v3.tvp.pl JSON listing."""
        PAGE = 100  # liczba vide na stonę
        PAGE = None  # wszystko na raz na stronie

        # TODO:  determine `view`
        with self.site.concurrent() as con:
            con.a.data.listing(id, count=PAGE, page=page)
            con.a.details.details(id)
        data = con.a.data
        details = con.a.details
        etype = details.get('object_type')

        with self.directory(view='movies') as kdir:
            if self.settings.debugging:
                if page is None:
                    kdir.item(f'=== {id}', call(self.enter_listing, id=id))  # XXX DEBUG
                else:
                    kdir.item(f'=== {id}, page {page}', call(self.enter_listing, id=id))  # XXX DEBUG
            items = data.get('items') or ()
            # if items:
            #     parents = items[0]['parents'][1:]
            #     if parents:
            #         kdir.menu('^^^', call(self.listing, id=parents[0]))  # XXX DEBUG
            if len(items) == 1 and items[0].get('object_type') == 'directory_video' and items[0]['title'] == 'wideo':
                # Oszukany katalog sezonu, pokaż id razu odcinki.
                data = self.site.listing(items[0]['asset_id'])
                items = data.get('items') or ()

            # ogromne katalogi > 100
            if PAGE and page is None:
                if data.get('total_count') and data['total_count'] > PAGE:
                    count = data['total_count']
                    for n in range((count + PAGE - 1) % PAGE):
                        if etype == 'directory_video':
                            kdir.menu(f'Strona {n+1}', call(self.listing, id=id, page=n+1, type='video'))
                        else:
                            kdir.menu(f'Strona {n+1}', call(self.listing, id=id, page=n+1))
                    return

            items = [item for item in items if item.get('object_type') in self.TYPES_ALLOWED]

            # Analiza szcegółów, w tym dokładnych opisów i danych video
            if self.settings.api_details:
                has_extra = False
                with self.site.concurrent() as con:
                    for item in items:
                        if 'asset_id' in item and item.get('object_type') == 'website':
                            iid = item['asset_id']
                            con.a[iid].details(iid)
                for item in items:
                    iid = item.get('asset_id')
                    if iid in con.a:
                        item['DETAILS'] = con.a[iid]
                        has_extra = True
                # Analiza video dostępnych pośrednio przez powyższe `DETAILS`
                if has_extra:
                    with self.site.concurrent() as con:
                        for item in items:
                            try:
                                iid = item['asset_id']
                                if item.get('DETAILS', {}).get('directory_video'):
                                    vdir = item['DETAILS']['directory_video']
                                    if len(vdir) == 1:
                                        vid = item['DETAILS']['directory_video'][0]['_id']
                                        con.a[iid].listing(vid)
                                        item['VIDEO_DIRECTORY'] = vid
                            except (KeyError, IndexError):
                                pass
                    for item in items:
                        iid = item.get('asset_id')
                        if iid in con.a:
                            item['VIDEOS'] = [it['asset_id'] for it in con.a[iid].get('items', ())
                                              if it.get('object_type') == 'video' and it.get('playable')]

            # Zwykłe katalogi (albo odcinki bezpośrenio z oszukanego).
            for item in items:
                self._item(kdir, item)

    # XXX  Jeszcze nieużywane
    EXTRA_TV = [
        53795158,  # TVP Kobieta
        53415775,  # Jasna Góra
        55643356,  # Kamera H
        16047094,  # Senat
        51696824,  # TVP Polonia
        56337313,  # TVP Polonia 2
        55989844,  # TVP Polonia OBS
        50930885,  # TVP Polonia OBS
        51696827,  # TVP Sport
        51696825,  # TVP Rozrywka
    ]

    def channel_iter_stations(self):
        """TV channel list."""
        # Regionalne: 38345166 → vortal → virtual_channel → live_video_id
        for item in self.site.stations():
            image = self._item_image(item, preferred='image_square')
            name, code = item['name'], item.get('code', '')
            yield ChannelInfo(code=code, name=name, image=image, id=item.get('id'))

    @entry(title=L(30123, 'Live TV'))
    def tv(self):
        """TV channel list."""
        # Regionalne: 38345166 → vortal → virtual_channel → live_video_id
        with self.directory() as kdir:
            for ch in self.channel_iter_stations():
                title = ch.name
                if self.settings.debugging:
                    title += f' [COLOR gray][{ch.code}][/COLOR]'
                kdir.play(title, call(self.station, ch.code), image=ch.image)

    @entry(title=L(30106, 'TV (HBB)'))
    def tv_hbb(self):
        """TV channel list."""
        with self.directory() as kdir:
            for ch in self.channel_iter():
                title = f'{ch.name} [COLOR gray][{ch.code or ""}][/COLOR]'
                if ch.code:
                    kdir.play(title, call(self.station, ch.code), image=ch.img)
                else:
                    title += f' [COLOR gray]{ch.id}[/COLOR]'
                    kdir.play(title, call(self.video, ch.id), image=ch.img)

    @entry(title=L(30107, 'TV (tv-stations)'))
    def tv_stations(self):
        """TV channel list."""
        with self.directory() as kdir:
            for item in self.site.jget('https://tvpstream.tvp.pl/api/tvp-stream/program-tv/stations')['data']:
                image = self._item_image(item, preferred='image_square')
                name, code = item['name'], item.get('code', '')
                kdir.play(f'{name} [COLOR gray][{code}][/COLOR]', call(self.station, code), image=image)

    @entry(title=L(30108, 'TV (html)'))
    def tv_html(self):
        """TV channel list."""
        with self.directory() as kdir:
            html = self.site.txtget('https://www.tvp.pl/program-tv')
            stations = html_json(html, 'window.__stations', strict=False)
            programs = list(html_json_iter(html, r'window.__stationsProgram\[\d+\]', strict=False))
            for item in html_json(html, 'window.__stationsData', strict=False).values():
                name, code = item['name'], item.get('code', '')
                extra = ''
                if any(it['code'] == code for it in stations):
                    extra += ' S'
                if any(it['station']['code'] == code for it in programs):
                    extra += ' P'
                img = item['logo_src']
                kdir.play(f'{name} [COLOR gray][{code}][/COLOR]{extra}', call(self.station, code), image=img)

    @entry(title=L(30109, 'TV (drzewo)'))
    def tv_tree(self):
        # recurse scan tree
        live, to_get = [], [68970]
        # filter_data = json.dumps({"playable": True})
        while to_get:
            log(f'tv_tree({to_get})...')
            with self.site.concurrent() as con:
                for pid in to_get:
                    # con.jget(None, params={'direct': True, 'count': '', 'parent_id': pid, 'filter': filter_data})
                    con.listing(pid, count=UNLIMITED)
            to_get = []
            for data in con:
                for item in data.get('items') or ():
                    if item['object_type'] in ('video', 'epg_item'):
                        if item.get('playable'):
                            live.append(item)
                    elif 'asset_id' in item:
                        to_get.append(item['asset_id'])
        # combine the same channels
        retitle = re.compile(r'^(?:\d+\s*)?(?:(TVP)\s*3\b)?(.*?)(?:\s+hd)?(?:\s*\(?(?:hbbtv|hbb)\)?)?\s*$',
                             re.IGNORECASE)
        tv, to_get = {}, []
        for item in live:
            # log(safefmt(('TV(tree): id={asset_id!r}, vid={video_id!r}, live={live_video_id!r}, playable={playable!r},'
            #              ' video_format={video_format_len}, videoFormatMimes={videoFormatMimes_len}, title={title!r}'),
            #             video_format_len=len(item.get('video_format', [])),
            #             videoFormatMimes_len=len(item.get('videoFormatMimes', [])), **item))
            title = retitle.sub(r'\1\2', item['title'].replace('Wlkp.', 'Wielkopolski'))
            if 'live_video_id' in item:
                to_get.append(item['asset_id'])
            tv.setdefault(title, []).append(item)
        # receive video formats pointed by 'live_video_id', extend 'videoFormatMimes'
        with self.site.concurrent() as con:
            for vid in to_get:
                con.a[vid].details(vid)
        videos = dict(con.a)
        for items in tv.values():
            for item in items:
                if 'live_video_id' in item:
                    item.setdefault('videoFormatMimes', []).extend(videos.get('videoFormatMimes', []))
        # filter out if no 'video_format'
        tv = {title: [item for item in items if item.get('videoFormatMimes')] for title, items in tv.items()}
        tv = {title: items for title, items in tv.items() if items}
        # build kodi directory list
        with self.directory(isort='label') as kdir:
            for title, items in tv.items():
                title += f" : [COLOR yellow]{','.join(str(it['asset_id']) for it in items)}[/COLOR]"
                self._item(kdir, items[0], title=title, debug=True)
                log(title)

    @entry(path='/replay', title=L(30115, 'Archive'))
    def replay_list(self):
        with self.directory() as kdir:
            for item in self.site.stations():
                image = self._item_image(item, preferred='image_square')
                name, code = item['name'], item.get('code', '')
                kdir.menu(name, call(self.replay_channel, code), image=image)

    @entry(path='/replay/<code>')
    def replay_channel(self, code):
        now = datetime.now()
        ar_date = [now - timedelta(days=n) for n in range(7)]
        with self.directory() as kdir:
            for date in ar_date:
                label = f'{date:%Y-%m-%d}'
                kdir.menu(label, call(self.replay_date, code=code, date=f'{date:%Y%m%d}'))

    @entry(path='/replay/<code>/<date>')
    def replay_date(self, code, date):
        def hm(t):
            return f'{datetime.fromtimestamp(t / 1000):%H:%M}'

        now_msec = int(datetime.now().timestamp() * 1000)  # TODO handle timezone
        with self.directory() as kdir:
            for epg in self.site.station_epg(code, date):
                if epg['date_start'] < now_msec:
                    pid = epg['record_id']
                    prog = epg.get('program', {})
                    cycle = prog.get('cycle', {})
                    img = self._item_image(cycle)
                    title = epg['title']
                    title = f'[{hm(epg["date_start"])} : {hm(epg["date_end"])}] {title}'
                    # title = f'[{hm(epg["date_start"])}] {title}'
                    kdir.play(title, call(self.play_program, code=code, prog=pid), image=img, descr=epg['description'])

    def play_hbb(self, id: PathArg, code: PathArg = ''):
        ...

    def play_program(self, code, prog):
        streams = self.site.station_streams(code, prog)
        if streams:
            stream = self.get_stream_of_type(streams)
            self._play(stream)

    def station(self, code: PathArg):
        data = self.site.jget('https://tvpstream.tvp.pl/api/tvp-stream/stream/data',
                              params={'station_code': code}).get('data')
        if data:
            stream = self.get_stream_of_type(self.site.jget(data['stream_url']).get('formats') or (), end=True)
            self._play(stream)

    def _play(self, stream):
        log(f'PLAY {stream!r}')
        from inputstreamhelper import Helper
        is_helper = Helper(stream.proto)
        if is_helper.check_inputstream():
            play_item = xbmcgui.ListItem(path=stream.url)
            if stream.mime is not None:
                play_item.setMimeType(stream.mime)
            play_item.setContentLookup(False)
            play_item.setProperty('inputstream', is_helper.inputstream_addon)
            play_item.setProperty("IsPlayable", "true")
            play_item.setProperty('inputstream.adaptive.manifest_type', stream.proto)
            xbmcplugin.setResolvedUrl(handle=self.handle, succeeded=True, listitem=play_item)

    def _item_start_time(self, item):
        start = item.get('release_date_long', item.get('broadcast_start_long', 0)) / 1000
        if not start:
            return None
        start = datetime.fromtimestamp(start) - self.tz_offset
        return start

    def _item_end_time(self, item):
        end = datetime.utcfromtimestamp(item.get('release_date_long',
                                                 item.get('broadcast_start_long', 0)) / 1000)
        end = item.get('broadcast_end_date_long', 0) / 1000
        if not end:
            return None
        end = datetime.fromtimestamp(end) - self.tz_offset
        return end

    def transmissions(self, id: PathArg, date=None):
        """Live transmistions (sport: 13010508, parlament: 4422078)."""
        now = datetime.utcnow()
        local_now = now + self.tz_offset
        local_prev = datetime.fromtimestamp(86400)
        layout = self.settings.transmission_layout
        if date:
            date = str2date(date)
        with self.directory() as kdir:
            # Reverse reversed order - get from current to future.
            for item in self.site.transmissions_items(id, filter=CurrentAndFuture):
                # Only current and future
                end = self._item_end_time(item)
                if end:
                    # log(f'now={now}, @now={local_now}, end={end} ({end + self.tz_offset}), live={item.get("is_live")}', title='===TIME===')
                    if item.get('is_live') and end + self.site.dT >= now:
                        start = self._item_start_time(item)
                        local_start = start + self.tz_offset
                        # log(f'now={now}, @now={local_now}, start={start} ({local_start}, end={end}', title='TIME')
                        if date is None or local_start.date() == date:
                            if date is not None or layout == TransmissionLayout.SingleList:
                                self._item(kdir, item)
                            elif layout == TransmissionLayout.DayFolder:
                                if local_start.date() != local_prev.date():
                                    title = day_label(local_start, now=local_now)
                                    kdir.menu(title, call(self.transmissions, id, date=local_start.date()))
                            elif layout == TransmissionLayout.DayLabel:
                                if local_start.date() != local_prev.date():
                                    title = day_label(local_start, now=local_now)
                                    kdir.separator(title, folder=call(self.transmissions, id, date=local_start.date()))
                                self._item(kdir, item, single_day=True)
                            local_prev = local_start

    def _item_image(self, item, *, preferred=None):
        if item is None:
            return None
        if preferred is None:
            preferred = ()
        elif isinstance(preferred, str):
            preferred = (preferred,)
        image = None
        for img_attr in (*preferred, 'image', 'image_16x9', *(key for key in item if key.startswith('image_'))):
            images = item.get(img_attr) or ()
            if isinstance(images, Mapping):
                images = (images,)
            for img_data in images:
                image = image_link(img_data)
                if image:
                    return image

    def _item(self, kdir, item, *, custom=None, title=None, single_day=False):
        """
        Parameters
        ----------
        single_day : bool
            True if start time is single day (time only).
        """
        def get_lead(name):
            v = item.get(name, '')
            return '' if v.startswith('!!!') else v

        itype = item.get('object_type')
        iid = item.get('asset_id', item.get('id', item.get('_id')))
        playable = item.get('playlable')
        details = item.get('DETAILS', {})
        style = None
        # format title
        if title is None:
            title = item.get('title', item.get('name', f'#{iid}'))
            if item.get('website_title'):
                title = f'{item["website_title"]}: {title}'
            elif item.get('title_root'):
                title = item['title_root']
        if title[:1].islower():
            title = title[0].upper() + title[1:]
        if self.settings.debugging:
            title = f'{title} [COLOR gray]({itype or "???"})[/COLOR]'  # XXX DEBUG
        # broadcast time
        now = datetime.utcnow()
        start = self._item_start_time(item)
        end = self._item_end_time(item)
        if start and end:
            if end + self.site.dT > now:
                future = start > now
                if single_day or start.date() == now.date() or start + timedelta(hours=6) < now:
                    # the same day or next 6h: only HH:MM
                    time = f'[{start + self.tz_offset:%H:%M}]'
                else:
                    # more then 6h: yyyy.mm.dd HH:MM
                    time = f'[{start + self.tz_offset:%Y.%m.%d %H:%M}]'
                if future:
                    time = self.format_title(time, ['COLOR gray'])
                elif end < now:
                    time = self.format_title(time, ['COLOR red'])  # transmisja powinna być zakończona, do 5min
                title = f'{time} {title}'
        elif start and start > now:
            if item.get('paymethod') and self.settings.email and self.settings.password:
                prefix = self.format_title('[P]', ['COLOR gold'])
            elif start.date() == now.date():
                prefix = self.format_title(f'[{start + self.tz_offset:%H:%M}]', ['COLOR gray'])
            else:
                prefix = self.format_title(f'[{start + self.tz_offset:%Y.%m.%d}]', ['COLOR gray'])
            title = f'{prefix} {title}'
        # image
        image = self._item_image(item)
        # description
        descr = (get_lead('lead_root') or item.get('description_root')
                 or get_lead('lead') or item.get('description_') or '')
        if 'commentator' in item:
            descr += '\n\n[B]Komentarz[/B]\n' + item['commentator']
        for cue in details.get('cue_card') or ():
            for par in cue.get('text_paragraph_standard') or ():
                descr += '[CR]{}'.format(par.get('text', '').replace(r'\n', '[CR]'))
        descr = remove_tags(descr)
        if 'release_date_dt' in item:
            descr += f"[CR][CR]{item['release_date_dt']} {item.get('release_date_hour', '')}"
        # menu
        menu = []
        if self.settings.debugging:
            menu.append(('!!!', self.exception))
            menu.append((f'ID {iid}', self.refresh))
            menu.append((f'Playlable {playable}', self.refresh))
            for pid in item.get('parents', ()):
                # menu.append((f'Parent {pid}', call(self.refresh, call(self.listing, id=pid))))
                menu.append((f'Parent {pid}', call(self.listing, id=pid)))
            attrs = ('video_id', 'virtual_channel_id', 'live_video_id', 'vortal_id')
            for attr in attrs:
                if attr in item:
                    menu.append((f'Go {attr} {item[attr]}', call(self.listing, id=item[attr])))
            # for attr in attrs:
            #     if attr in item:
            #         menu.append((f'Play {attr} {item[attr]}', call(self.video, id=item[attr])))
        if 'SERIES' in item:
            series = item['SERIES']
            menu.append((series['title'], self.cmd.Container.Update(call(self.listing, series['id']))))
        # item
        position = 'top' if itype == 'directory_toplist' else None
        if position and not style:
            style = ['COLOR :spec', 'B']
        kwargs = dict(image=image, descr=descr, custom=custom, position=position, menu=menu, style=style)
        if itype in ('video', 'epg_item'):
            # if 'virtual_channel_id' in item:
            #     iid = item['virtual_channel_id']
            # elif
            if 'video_id' in item:
                iid = item['video_id']
            kdir.play(title, call(self.video, id=iid), **kwargs)
        elif itype == 'virtual_channel':
            kdir.menu(title, call(self.transmissions, id=iid), **kwargs)
        else:
            if item.get('VIDEOS'):
                if len(item['VIDEOS']) == 1:
                    vid = item['VIDEOS'][0]
                    kdir.play(title, call(self.video, id=vid), **kwargs)
                    return
                iid = item.get('VIDEO_DIRECTORY', iid)
            kdir.menu(title, call(self.listing, id=iid), **kwargs)

    def video_eu(self, id: PathArg[str]):
        """Play EU video. `id` is euro-video-id or url."""
        def langkey(v):
            L, S = v['language'], v['subtitles']
            if S:
                return f'{L}/{S}'
            return L

        if isinstance(id, URL) or '://' in id:
            url = URL(id)
            log(f'EU !! {url}')
            if 'MFEmbeded' in id or 'EmbedPlayer' in id:
                # resp = self.site.txtget(url, allow_redirects=True)
                # r = re.search(r'a="(?P<a>\d+)",s="(?P<s>\d+)",l="(?P<l>\d+)",c="(?P<c>[^"]*)"', resp)
                # if r:
                #     S, L = r.group('s', 'l')
                #     url = f'https://kmc.europarltv.europa.eu/p/{S}/sp/{S}00/embedIframeJs/uiconf_id/{L}/partner_id/{S}'
                xbmcgui.Dialog().notification('TVP', 'Embedded player jest nieobsługiwany',
                                              xbmcgui.NOTIFICATION_INFO)
                return self.play_failed()
            resp = self.site.head(url, allow_redirects=False)
            if 300 <= resp.status_code <= 399:
                url = URL(resp.headers['location'])
            id = url.path.rpartition('/')[2]
        data = self.site.jget('https://api.multimedia.europarl.europa.eu/o/epmp-frontend-rest/v1.0/getinfo',
                              params={'mediaBusinessID': id})
        videos = data.get('resultJSON', {}).get('content', {}).get('videos', [])
        langs = {langkey(v): sorted(EuVideo(*s['resolution'].split('x'), s['bitRate'], s['url'])
                                    for s in v['resolutions']) for v in videos}
        log(f'Play EU: id={id!r}, langs={len(langs)}, pl={"pl" in langs}')
        url = None
        for lang in ('pl', 'en/pl', 'en'):
            video = langs.get(lang)
            if video:
                url = video[0].url
                break
        else:
            # any language
            if langs:
                video = next(iter(langs.values()))
                if video:
                    url = video[0].url
        if url:
            item = xbmcgui.ListItem(path=url)
            item.setProperty("IsPlayable", "true")
            xbmcplugin.setResolvedUrl(self.handle, True, listitem=item)
        else:
            self.play_failed()

    def play_failed(self):
        item = xbmcgui.ListItem()
        xbmcplugin.setResolvedUrl(self.handle, False, listitem=item)

    def video(self, id: PathArg[int]):
        """Play video – PlayTVPInfo by mtr81."""
        # TODO: cleanup
        data = self.site.details(id)
        log(f"Video: {id}, type={data.get('type')}, live_video_id={data.get('live_video_id')},"
            f" video_id={data.get('video_id')}", title='TVP')
        ###!!! if data.get('type') == 'virtual_channel' and 'live_video_id' in data:
        ###!!!     id = data['live_video_id']
        start = self._item_start_time(data)
        end = self._item_end_time(data)
        if start:
            now = datetime.utcnow()
            if not end:
                try:
                    end = start + timedelta(seconds=data['duration'])
                except KeyError:
                    end = now + timedelta(days=1)
            # if not start < now < end:  # sport only current
            if not data.get('paymethod') and start > now:  # future
                xbmcgui.Dialog().notification('TVP', 'Transmisja niedostępna teraz', xbmcgui.NOTIFICATION_INFO)
                xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())
                log(f'Video {id} in future: {start} > {now}', title='TVP')
                return

        # Euro-parlament
        if 'europarltv' in data.get('url', ''):
            iframe = data.get('html_params', [{}])[0].get('text')
            if iframe and ('<iframe' in iframe or '<object' in iframe):
                r = re.search(r'src="([^"]*)"', iframe)
                if r:
                    # pass URL to video_eu
                    return self.video_eu(r.group(1))

        if 'video_id' in data:
            id = data['video_id']
        # url = f'https://www.tvp.pl/shared/cdn/tokenizer_v2.php?object_id={id}&sdt_version=1&time_shift=true&end='
        url = f'https://www.tvp.pl/shared/cdn/tokenizer_v2.php?object_id={id}&sdt_version=1&time_shift=true'
        # url = f'https://www.tvp.pl/shared/cdn/tokenizer.php?object_id={id}&time_shift=true&end='
        url = f'https://www.tvp.pl/shared/cdn/tokenizer_v2.php?object_id={id}'
        resp = self.site.jget(url)
        stream_url = ''
        if resp['payment_type'] != 0 or resp['status'] == 'NOT_FOUND_FOR_PLATFORM':  # ABO
            hea = {
                'accept-encoding': 'gzip',
                'authorization': 'Basic dGVzdHZvZDp0ZXN0eXZvZDI5Mng=',
                'connection': 'Keep-Alive',
                'content-type': 'application/x-www-form-urlencoded',
                'user-agent': 'okhttp/3.8.1',
            }
            # not setting defined yet
            data = {
                'client_id': 'vod-api-android',
                'username': self.settings.email,
                'client_secret': 'Qao*kN$t10',
                'grant_type': 'password',
                'password': self.settings.password,
            }
            resp = self.site.jpost('http://www.tvp.pl/sess/oauth/oauth/access_token.php', headers=hea, data=data)
            token = ''
            log(f'TVP oauth resp: {resp!r}', title='ABO')
            if 'error' in resp:
                if resp['error'] == 'invalid_credentials':
                    xbmcgui.Dialog().notification('[B]Błąd[/B]',
                                                  ('[Strefa ABO] Dostęp do materiału po wpisaniu danych dostępowych'
                                                   ' w zakładce ustawienia.'),
                                                  xbmcgui.NOTIFICATION_INFO, 8000, False)
            else:
                token = resp['access_token']
                hea = {
                    'accept-encoding': 'gzip',
                    'authorization': 'Basic YXBpOnZvZA==',
                    'connection': 'Keep-Alive',
                    'content-length': '0',
                    'content-type': 'application/x-www-form-urlencoded',
                    'user-agent': 'okhttp/3.8.1',
                    'access-token': token
                }
                resp = self.site.jpost(f'https://apivod.tvp.pl/tv/v2/video/{id}/default/default?device=android',
                                       headers=hea, verify=False)
                if resp['success'] == 0:
                    xbmcgui.Dialog().notification('[B]Błąd[/B]', '[Strefa ABO] Brak uprawnień', xbmcgui.NOTIFICATION_INFO, 8000, False)
                else:
                    for d in resp['data']:
                        if 'id' in d:
                            if d['id'] == id:
                                subt = self.subt_gen_ABO(d)
                                if d['is_drm'] is True:  # DRM
                                    url_stream = re.findall('fileDash\': \'([^\']+?)\'', str(resp))[0]
                                    licUrl = re.findall('proxyWidevine\': \'([^\']+?)\'', str(resp))[0]
                                    # print(url_stream)
                                    # print(licUrl)
                                    if url_stream and licUrl:
                                        import inputstreamhelper
                                        PROTOCOL = 'mpd'
                                        DRM = 'com.widevine.alpha'
                                        is_helper = inputstreamhelper.Helper(PROTOCOL, drm=DRM)
                                        if is_helper.check_inputstream():
                                            play_item = xbmcgui.ListItem(path=url_stream)
                                            play_item.setSubtitles(subt)
                                            play_item.setProperty("inputstream", is_helper.inputstream_addon)
                                            play_item.setProperty("inputstream.adaptive.manifest_type", PROTOCOL)
                                            play_item.setContentLookup(False)
                                            play_item.setProperty("inputstream.adaptive.license_type", DRM)
                                            play_item.setProperty("inputstream.adaptive.license_key", licUrl+'||R{SSM}|')
                                            xbmcplugin.setResolvedUrl(self.handle, True, listitem=play_item)

                                            return
                                else:  # non-DRM
                                    for f in d['formats']:
                                        if f['mimeType'] == 'application/x-mpegurl':
                                            stream_url = f['url']
                                            break
                                    play_item = xbmcgui.ListItem(path=stream_url)
                                    play_item.setProperty('IsPlayable', 'true')
                                    play_item.setSubtitles(subt)
                                    xbmcplugin.setResolvedUrl(self.handle, True, listitem=play_item)

        else:  # free
            log(f'free video: {id}', title='TVP')
            stream = Stream(stream_url, '', '')
            if 'formats' in resp:
                stream = self.get_stream_of_type(resp['formats'], end=False)
                if stream_url is not None:
                    if (stream.mime == 'application/x-mpegurl' and 'end' in stream.url.query
                            and '.m3u8' in str(stream.url) and not self.site.head(stream.url).ok):
                        # remove `end` if error
                        log(f'Remove `end` from {url!r}')
                        url = stream.url
                        url = url.with_query([(k, v) for k, v in url.query.items() if k != 'end'])
                        stream = stream._replace(url=url)
                    # XXX TEST
                    # subt = self.subt_gen_free(id)
                    play_item = xbmcgui.ListItem(path=str(stream.url))
                    play_item.setProperty('IsPlayable', 'true')
                    # play_item.setSubtitles(subt)
                    log(f'PLAY!: handle={self.handle!r}, url={stream!r}', title='TVP')
                    return self._play(stream)
                # for stream_url in self.iter_stream_of_type(resp['formats'], end=False):
                #     resp = self.site.head(stream_url.url).status_code
                #     log(f'SSSSSSSSSS {resp.status_code!r} for {stream_url!r}')
                #     if resp.ok:
                #         break
                # else:
                    xbmcgui.Dialog().notification('[B]Błąd[/B]', 'Brak strumienia do odtworzenia.',
                                                  xbmcgui.NOTIFICATION_INFO, 3000, False)
                    xbmcplugin.setResolvedUrl(self.handle, False, listitem=xbmcgui.ListItem())
                    return
            subt = self.subt_gen_free(id)
            play_item = xbmcgui.ListItem(path=str(stream.url))
            play_item.setProperty('IsPlayable', 'true')
            play_item.setSubtitles(subt)
            log(f'PLAY: handle={self.handle!r}, url={stream!r}', title='TVP')
            xbmcplugin.setResolvedUrl(self.handle, True, listitem=play_item)

    def subt_gen_ABO(self, d):
        """Tablica z linkami do plików z napisami (format .ssa)."""
        subt = []
        if 'subtitles' in d:
            if d['subtitles']:
                path: Path = self.profile_path / 'temp'
                path.mkdir(parents=True, exist_ok=True)
                for n, it in enumerate(d['subtitles']):
                    urlSubt = it['src']
                    resp = self.site.get(urlSubt)
                    ttml = Ttml2SsaAddon()
                    ttml.parse_ttml_from_string(resp.text)
                    ttml.write2file(path / f'subt_{n+1:02d}.ssa')
                    subt.append(path / f'subt_{n+1:02d}.ssa')
        return subt

    def subt_gen_free(self, aId):
        """Tablica z linkami do plików z napisami (format .ssa)."""
        if Ttml2SsaAddon is None:  # XXX DEBUG only
            return []
        url = URL('https://vod.tvp.pl/sess/TVPlayer2/api.php?id={aId}&@method=getTvpConfig&@callback=?')
        resp = self.site.get(url).text
        resp = re.findall(r'_\((.*)\)', resp, re.DOTALL)[0]
        if resp.startswith('null,'):
            log.info(f'No subtiles for {aId!r}', title='TVP')
            return []
        try:
            data = json.loads(resp)['content']
        except Exception as exc:
            log.warning(f'Subtiles JSON failed {exc} on {resp!r}', title='TVP')
            return []
        subt = []
        path: Path = self.profile_path / 'temp'
        path.mkdir(parents=True, exist_ok=True)
        if 'subtitles' in data and len(data['subtitles']):
            for n, d in enumerate(data['subtitles']):
                urlSubt = url.join(d['url'])
                ttml = Ttml2SsaAddon()
                ttml.parse_ttml_from_string(self.site.get(urlSubt).text)
                ttml.write2file(path / f'subt_{n+1:02d}.ssa')
                subt.append(path / f'subt_{n+1:02d}.ssa')
        return subt

    def all_tv(self):
        ...

    def hbb_api(self, query):
        """Request query and returns JSON."""
        query = {
            "operationName": None,
            "variables": {
                "categoryId": None,
            },
            # "extensions": {
            #     "persistedQuery": {
            #         "version": 1,
            #         "sha256Hash": "5c29325c442c94a4004432d70f94e336b8c258801fe16946875a873e818c8aca",
            #     },
            # },
            "query": query,
        }
        return self.site.jpost('https://hbb-prod.tvp.pl/apps/manager/api/hub/graphql', json=query)

    def channel_iter_hbb(self):
        """JSON-live channel list."""
        data = self.hbb_api('''
            query {
                getStationsForMainpage {
                    items {
                        id
                        name
                        code
                        image_square {
                            url
                            width
                            height
                        }
                    }
                }
            }''')
        log(f'data\n{data!r}')
        re_name = re.compile(r'^(?:EPG(?:\s*-\s*)?)?\s*([^\d]+?)\s*(\d.*)?\s*$')
        for ch in data['data']['getStationsForMainpage']['items']:
            code = ch['code']
            name = ' '.join(s for s in re_name.search(ch['name']).groups() if s)
            imgdata = ch['image_square']
            width, height = imgdata['width'], imgdata['height']
            if not code:
                ch_id = ch['id']
                if not width or not height:
                    width = height = 1000
            else:
                ch_id = ''
                if not width or not height:
                    width = height = 140
            img = imgdata['url'].format(width=width, height=height)
            yield ChannelInfo(code=code, name=name, image=img, id=ch_id)

    @search.folder
    def search_bestresults(self, query, options=None):
        url = f'https://sport.tvp.pl/api/tvp-stream/search?query={query}&scope=bestresults&page=1&limit=&device=android'
        with self.directory() as kdir:
            items = self.site.jget(url).get('data', {}).get('occurrenceitem', ())
            with self.site.concurrent() as con:
                indexes = [con.details(item['id']) for item in items if 'id' in item]
            items = [{**con[i], **{'FOUND': found}} for i, found in zip(indexes, items)]
            for item in items:
                ### XXX log(f'SEARCH item: \n{json.dumps(item)}')
                cycle = item.get('FOUND', {}).get('program', {}).get('cycle', {})
                if cycle and cycle.get('title'):
                    item['SERIES'] = {
                        'id': item['parents'][0],
                        'title': cycle['title'],
                        'image_logo': cycle.get('image_logo'),
                    }
                self._item(kdir, item)

    @staticmethod
    def iter_stream_of_type(streams, *, end=False):
        mime_types = {
            'application/vnd.ms-ss': StreamType('ism', 'application/vnd.ms-ss'),
            'video/mp4':             StreamType('hls', 'application/x-mpegURL'),
            'video/mp2t':            StreamType('hls', 'application/x-mpegURL'),
            'application/dash+xml':  StreamType('mpd', 'application/dash+xml'),
            'application/x-mpegurl': StreamType('hls', 'application/x-mpegURL'),
        }

        for st in streams:
            for prio, mime in enumerate(mime_types):
                if st['mimeType'] == mime:
                    st['priority'] = prio

        streams = sorted(streams, key=lambda d: ((d['priority']), -int(d['totalBitrate'])), reverse=True)
        for st in streams:
            if 'material_niedostepny' not in st['url']:
                for mime, stype in mime_types.items():
                    if st['mimeType'] == mime:
                        url = URL(st['url'])
                        if end and 'end' not in url.query:
                            url = url % {'end': ''}
                        yield Stream(url=url, proto=stype.proto, mime=stype.mime)

    @staticmethod
    def get_stream_of_type(streams, *, end=False):
        for stream in TvpPlugin.iter_stream_of_type(streams, end=end):
            return stream

    def exception(self):
        raise RuntimeError()

    # Generator m3u – do zaorania
    # TODO: make generator in the libka
    def build_m3u(self):
        path_m3u = self.settings.m3u_folder
        file_name = self.settings.m3u_filename

        if not file_name or not path_m3u:
            xbmcgui.Dialog().notification('TVP', L(30132, 'Set filename and destination directory'),
                                          xbmcgui.NOTIFICATION_ERROR)
            return

        xbmcgui.Dialog().notification('TVP', L(30134, 'Generate playlist'), xbmcgui.NOTIFICATION_INFO)
        data = '#EXTM3U\n'

        for ch in self.channel_iter_stations():
            url = self.mkurl(self.station, code=ch.code)
            data += f'#EXTINF:0 tvg-id="{ch.name}" tvg-logo="{ch.image}" group-title="TVP",{ch.name}\n{url}\n'

        try:
            f = xbmcvfs.File(path_m3u + file_name, 'w')
            f.write(data)
        finally:
            f.close()
        xbmcgui.Dialog().notification('TVP', L(30135, 'Playlist M3U generated'), xbmcgui.NOTIFICATION_INFO)


# DEBUG ONLY
import sys  # noqa
log(f'\033[1mTVP\033[0m: \033[93mENTER\033[0m: {sys.argv}')

# Create and run plugin.
TvpPlugin().run()
