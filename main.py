from libka import L, Plugin, Site, subobject
from libka import call, PathArg, entry
from libka.logs import log
from libka.url import URL
from libka.path import Path
from libka.menu import Menu, MenuItems
from libka.utils import html_json, html_json_iter
from libka.lang import day_label, text as lang_text
from libka.calendar import str2date
from libka.search import search, Search
from libka.settings import Settings
from pdom import select as dom_select
from urllib.parse import urlencode, urlparse, parse_qs, quote
import json
from collections.abc import Mapping
from collections import namedtuple, UserList, UserDict
from html import unescape
from datetime import datetime, timedelta
import re
from enum import IntEnum
import xbmc  # for getCondVisibility and getInfoLabel
import xbmcgui  # dialogs
import xbmcplugin  # setResolvedUrl
import xbmcvfs  # for file in m3u generator
import requests

try:
    from ttml2ssa import Ttml2SsaAddon
except ModuleNotFoundError:
    Ttml2SsaAddon = None  # DEBUG only

# XXX
# Na razie wszystko jest w jednym pliku, bo łatwiej odświeżać w kodi.
# Potem poszczególne klasy wylądują w resources/lib/
# XXX

# Some "const" defines (as unique values).
MISSING = object()
UNLIMITED = object()
Future = object()
CurrentAndFuture = object()

KODI_VERSION = int(xbmc.getInfoLabel('System.BuildVersion')[:2])
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.5060.134 Safari/537.36 Edg/103.0.1264.71'

class TransmissionLayout(IntEnum):
    DayFolder = 0
    DayLabel = 1
    SingleList = 2


class TvIconType(IntEnum):
    TvLogo = 0
    EpgIcon = 1


class TvEntryFormat(IntEnum):
    Custom = 0
    TIME_CHAN_TITLE = 1
    CHAN_TIME_TITLE = 2
    CHAN_TITLE_TIME = 3

    @staticmethod
    def formats():
        return {
            TvEntryFormat.TIME_CHAN_TITLE: '{prog.times} {channel.name} – {prog.title}',
            TvEntryFormat.CHAN_TIME_TITLE: '{channel.name} {prog.times} – {prog.title}',
            TvEntryFormat.CHAN_TITLE_TIME: '{channel.name} – {prog.title}||{prog.times}',
        }

    @staticmethod
    def get(num, default='{prog.times} {channel.name} – {prog.title}'):
        return TvEntryFormat.formats().get(TvEntryFormat(num), default)


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

ChannelInfo = namedtuple('ChannelInfo', 'code name image id epg', defaults=(None,))
ChannelInfo.__str__ = lambda self: self.name


class Info(namedtuple('Info', 'data type url title image descr series linkid')):

    @classmethod
    def parse(cls, data):
        try:
            data = json.loads(unescape(data))
            e_link = data.get('episodeLink')
            s_link = data.get('seriesLink')
            url = URL(s_link)
            # 'episodeCount'
            # TODO:  dodać analizę w zlaezności od typu i różnic w obu linkach
            #        np. "video" i takie same linki wskazuję bezpośrednio film
            image = data['image']
            return Info(data, type=data['type'], url=url, title=data['title'], image=image,
                        descr=data.get('description'), series=(e_link != s_link),
                        linkid=url.path.rpartition(',')[2])
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            log.warning(f'Can not parse video info {exc} from: {data!r}')
            return None


class ChannelEpg(UserList):
    """EPG for a station."""

    def __init__(self, items, *, now=None):
        super().__init__(ChannelProgram(item) for item in items or ())
        if now is None:
            now = datetime.now()
        self.now = now
        self._current = MISSING
        self._next = MISSING

    @property
    def current(self):
        """Current program."""
        if self._current is MISSING:
            prog = max((prog for prog in self if prog.start <= self.now), default=None, key=lambda progs: progs.start)
            self._current = None if prog is None or prog.end < self.now else prog
        return self._current

    @property
    def next(self):
        """Next program."""
        if self._next is MISSING:
            prog = min((prog for prog in self if prog.start > self.now), default=None, key=lambda progs: progs.start)
            self._next = prog
        return self._next

    def __str__(self):
        return str(self.current or '')


class ChannelProgram(UserDict):
    """Single EPG program for a station."""

    def __init__(self, data):
        super().__init__(data)
        self.start = datetime.fromtimestamp(self['date_start'] / 1000)
        self.end = datetime.fromtimestamp(self['date_end'] / 1000)
        self.outline = self.get('description') or ''
        self.plot = self.get('description_long') or ''
        prog = self.get('program') or {}
        self.title = prog.get('title', self.data['title']) or ''
        self.descr = f'[B]{self.title}[/B][CR]{self.start:%Y-%m-%d %H:%M}-{self.end:%H:%M}[CR]{self.plot}'
        self.times = f'{self.start:%H:%M}-{self.end:%H:%M}'

    @property
    def date(self):
        return self.start

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def __str__(self):
        return self.title


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
                      filter_dict={'is_live': True}, order={'release_date_long': -1}, **kwargs):
        if filter_dict is CurrentAndFuture:
            filter_dict = f'broadcast_end_date_long>={(datetime.now() - self.dT).timestamp() * 1000}'
        elif filter_dict is Future:
            filter_dict = f'release_date_long>={(datetime.now() - self.dT).timestamp() * 1000}'
        return self.listing(parent_id, dump=dump, direct=direct, type=type, filter=filter_dict, order=order, **kwargs)

    # Dicts `filter` and `order` could be in arguments because they are read-only.
    def transmissions_items(self, parent_id, *, dump='json', direct=False, type='epg_item',
                            filter_dict={'is_live': True}, order={'release_date_long': -1}, **kwargs):
        data = self.transmissions(parent_id, dump=dump, direct=direct, type=type, filter_dict=filter_dict, order=order,
                                  **kwargs)
        # reverse reversed ('release_date_long': -1) list
        return reversed(data.get('items') or ())

    def details(self, object_id, *, dump='json', **kwargs):
        return self.jget('/shared/details.php',
                         params={'dump': dump, 'object_id': object_id, **kwargs})

    def stations(self):
        return self.jget('https://tvpstream.tvp.pl/api/tvp-stream/program-tv/stations').get('data') or ()

    def station_epg(self, station_code, date=None, **kwargs):
        if date is None:
            date = datetime.now()
        if not isinstance(date, str):
            date = f'{date:%Y-%m-%d}'
        return self.jget('https://tvpstream.tvp.pl/api/tvp-stream/program-tv/index', params={
            'station_code': station_code,
            'date': date,
            **kwargs,
        }).get('data') or ()

    def station_full_epg(self, station_code, date=None, **kwargs):
        epg = ChannelEpg(self.station_epg(station_code, date))
        with self.concurrent() as con:
            for prog in epg:
                con.occurrence(prog.id)
        # EPG `occurrence` sometime lay about start and end date
        # (SKIP occurrence OVERRIDE)    epg = ChannelEpg(ChannelProgram(data['data']) for data in con)
        epg = ChannelEpg(ChannelProgram({**occ['data'], 'date_start': prog['date_start'], 'date_end': prog['date_end']})
                         for prog, occ in zip(epg, con))
        return epg

    def station_program(self, station_code, record_id):
        return self.jget('https://tvpstream.tvp.pl/api/tvp-stream/stream/data', params={
            'station_code': station_code,
            'record_id': record_id,
        })

    def occurrence(self, id, *, device=None, **kwargs):
        # Used in EPG
        # device='android'
        if device is not None:
            kwargs['device'] = device
        return self.jget('https://tvpstream.tvp.pl/api/tvp-stream/program-tv/occurrence', params={'id': id, **kwargs})

    def station_streams(self, station_code, record_id):
        data = self.station_program(station_code=station_code, record_id=record_id)
        url = data.get('data', {}).get('stream_url')

        redir = self.jget(url)
        formats = redir.get('formats')
        mimetype = redir.get('mimeType')

        return formats, mimetype

    def blackburst(self, parent_id, *, dump='json', direct=False, type='video', nocount=1, copy=False,
                   filterx={'playable': True}, order='release_date_long,-1', release_date=None, **kwargs):
        count = kwargs.pop('count', self.count)
        if count is None or count is UNLIMITED:
            count = ''
        if kwargs.get('page', ...) is None:
            kwargs.pop('page')
        filterx = dict(filterx)
        if release_date:
            filterx['release_date_long'] = {'$lt': release_date.timestamp() * 1000}
        # filter['play_mode'] = 1
        return self.jget('/shared/listing_blackburst.php',
                         params={'dump': dump, 'direct': direct, 'count': count, 'parent_id': parent_id,
                                 'nocount': nocount, 'copy': copy, 'type': type, 'filter': filterx, 'order': order,
                                 **kwargs})


class TvpPlugin(Plugin):
    """tvp.pl plugin."""

    MENU = Menu(order_key='title', view='addons', items=[
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
            Menu(call='tv_program'),
            Menu(call='replay_list'),
        ]),
        MenuItems(id=1785454, type='directory_series', order={2: 'programy', 1: 'seriale', -1: 'teatr*'}),
        # Menu(title='Rekonstrucja cyfrowa', id=35470692),  --- jest już powyższym w MenuItems(1785454)
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
            Menu(title='TVP VOD', call='vod_search'),
            Menu(title='TVP GO', call='search'),
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

    NOT_ALLOWED = {
        'strona-glowna',
        'strona-druzyn-ligi',
        'strona-glowna-dyscypliny',
        'aktualnosci',
        'galeriee',
        'galerie',
        'galerie-zdjec',
        'galeria',
        'sg',
        'klasyfikacja-medalowa',
        'wyniki',
        'sidebars',
        'sidebary',
        'menu',
        'terminarz',
        'kadra-olimpijska',
        'ankieta',
        'testy',
        'wyniki-top',
        'statystyki-turnieju',
        'promocja-sport',
        'video-import'
    }
    vod_search = subobject()

    epg_url = 'http://www.tvp.pl/shared/programtv-listing.php?station_code={code}&count=100&filter=[]&template=json%2Fprogram_tv%2Fpartial%2Foccurrences-full.html&today_from_midnight=1&date=2022-04-25'

    def __init__(self):
        super().__init__()
        self.site = TvpSite()
        self.colors['spec'] = 'gold'
        self.formatter.default_formats.update({
            'prog.date': '%Y.%m.%d',
            'prog.start': '%H:%M',
            'prog.end': '%H:%M',
            'date': '%Y.%m.%d',
            'start': '%H:%M',
            'end': '%H:%M',
            'trans.time': {'one_day': '%H:%M', 'another_day': '%Y.%m.%d %H:%M'},
        })
        styles = self.settings.get_styles('tvp_time', 'tvp_chan', 'tvp_prog')
        self.styles.update({
            'prog.times': styles.tvp_time,
            'prog.start': styles.tvp_time,
            'prog.end': styles.tvp_time,
            'prog.date': styles.tvp_time,
            'times': styles.tvp_time,
            'start': styles.tvp_time,
            'end': styles.tvp_time,
            'date': styles.tvp_time,
            'channel': styles.tvp_chan,
            'tv': styles.tvp_chan,
            'prog.title': styles.tvp_prog,
            'title': styles.tvp_prog,
            # TODO: add to settings
            'trans.sep': 'COLOR gray;I'.split(';'),
            'trans.time': {
                None: '[]'.split(';'),
                'future': 'COLOR gray;[]'.split(';'),
                'finished': 'COLOR red;[]'.split(';'),
            },
            'folder_list_separator': ['COLOR khaki', 'B', 'I'],
        })
        self.vod_search = Search(addon=self, site=self.site, name='vod', method=self.vod_search_folder)

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

    def fmt(self, format, **kwargs):
        """ Format title (label) using styles. Split `label2` on `||`."""
        label, _, label2 = self.formatter.format(format, **kwargs).partition('||')
        return label, label2 or None

    def listing(self, id: PathArg[int], page=None, vid_type=None):
        """Use api.v3.tvp.pl JSON listing."""
        per_page = 100  # liczba video na stonę
        # PAGE = None  # wszystko na raz na stronie

        # TODO:  determine `view`
        with self.site.concurrent() as con:
            con.a.data.listing(id, count=per_page, page=page)
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
                # Oszukany katalog sezonu, pokaż od razu odcinki.
                data = self.site.listing(items[0]['asset_id'])
                items = data.get('items') or ()

            # ogromne katalogi > 100
            if per_page and page is None:
                if data.get('total_count') and data['total_count'] > per_page:
                    count = data['total_count']
                    for n in range((count + per_page - 1) % per_page):
                        if n == round(count / per_page):
                            break
                        else:
                            if etype == 'directory_video':
                                kdir.menu(f'Strona {n + 1}', call(self.listing, id=id, page=n + 1, vid_type='video'))
                            elif etype == 'website':
                                kdir.menu(f'Strona {n + 1}', call(self.listing, id=id, page=n + 1, vid_type='website'))
                            else:
                                kdir.menu(f'Strona {n + 1}', call(self.listing, id=id, page=n + 1))
                    return
            items = [item for item in items if
                     item.get('object_type') in self.TYPES_ALLOWED and item.get('web_name') not in self.NOT_ALLOWED]

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
                        if iid in con.a and con.a[iid]:
                            item['VIDEOS'] = [it['asset_id'] for it in con.a[iid].get('items', ())
                                              if it.get('object_type') == 'video' and it.get('playable')]

            # Zwykłe katalogi (albo odcinki bezpośrenio z oszukanego).
            if vid_type == 'website':
                with self.site.concurrent() as con:
                    con.a.data.listing(id)
                    con.a.details.details(id)
                data = con.a.data
                a_id = data['items'][0]['asset_id']
                items = self.site.listing(a_id, count=per_page, page=page).get('items')
                for item in items:
                    self._item(kdir, item)
            else:
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

    def channel_iter_stations(self, *, epg=False, now=None):
        """TV channel list."""
        # Regionalne: 38345166 → vortal → virtual_channel → live_video_id
        stations = self.site.stations()
        epgs = {}
        if epg:
            if now is None:
                now = datetime.now()
            with self.site.concurrent() as con:
                for item in stations:
                    code = item.get('code')
                    if code:
                        con[code].station_epg(code, date=now)
            epgs = {code: ChannelEpg(epg) for code, epg in con.items()}
            with self.site.concurrent() as con:
                for prog in epgs.values():
                    cur = prog.current
                    if cur:
                        con[cur.id].occurrence(cur.id)
            for prog in epgs.values():
                cur = prog.current
                if cur:
                    if cur.id:
                        prog._current = ChannelProgram({**con[cur.id]['data'], 'date_start': cur['date_start'], 'date_end': cur['date_end']})
        for item in stations:
            image = self._item_image(item, preferred='image_square')
            name, code = item['name'], item.get('code', '')
            name = re.sub(r"([0-9]+(\.[0-9]+)?)", r" \1", name).strip().replace('  ', ' ')
            yield ChannelInfo(code=code, name=name, image=image, id=item.get('id'), epg=epgs.get(code))

    @entry(title=L(30137, 'Program'))
    def tv_program(self):
        self.tv(program=True)

    @entry(title=L(30123, 'Live TV'))
    def tv(self, *, program=False):
        """TV channel list."""
        # Regionalne: 38345166 → vortal → virtual_channel → live_video_id
        title_format = TvEntryFormat.get(self.settings.tv_entry_format, self.settings.tv_entry_custom_format)
        with self.directory() as kdir:
            for ch in self.channel_iter_stations(epg=True):
                kwargs = {}
                prog = None
                title, image = ch.name, ch.image
                if ch.epg and ch.epg.current:
                    channel, prog = ch, ch.epg.current
                    # title = f'[{prog.times}] {channel.name} – {prog.title}'
                    if program:
                        title_format = channel.name

                    title, label2 = self.fmt(title_format, prog=prog, channel=channel, tv=channel.name,
                                             title=prog.title, times=prog.times, start=prog.start, end=prog.end,
                                             date=prog.date)
                    if label2:
                        kwargs['label2'] = label2
                    plot = f'[B]{ch.epg.current.title}[/B][CR]{ch.epg.current.outline}'
                    descr = ch.epg.current.descr
                    if ch.epg.next:
                        extra = f'[CR][CR]{ch.epg.next.start:%H:%M} – {ch.epg.next.title}'
                        plot += extra
                        descr += extra
                    kwargs['info'] = {
                        # 'outline': ch.epg.current.outline,
                        'plotoutline': plot,
                        'plot': descr,
                    }
                    kwargs['menu'] = [
                        (L(30137, 'Program'),
                         self.cmd.Container.Update(call(self.station_program, ch.code, f'{prog.start:%Y%m%d}'))),
                        (L(30115, 'Archive'), self.cmd.Container.Update(call(self.replay_channel, ch.code))),
                    ]
                    if self.settings.developing or self.settings.debugging:
                        kwargs['menu'].insert(0, ('!!!', self.exception))
                    if self.settings.tv_icon_type == TvIconType.EpgIcon:
                        eprog = prog.get('program') or {}
                        image = self._item_image(eprog, eprog.get('cycle'), default=image)
                if self.settings.debugging:
                    title += f' [COLOR gray][{ch.code}][/COLOR]'
                if program:
                    if prog:
                        kdir.menu(title, call(self.station_program, ch.code, f'{prog.start:%Y%m%d}'),
                                  image=image, **kwargs)
                else:
                    kdir.play(title, call(self.station, ch.code, '.pvr'), image=image, **kwargs)

    @entry(title=L(30106, 'TV (HBB)'))
    def tv_hbb(self):
        """TV channel list."""
        with self.directory() as kdir:
            for ch in self.channel_iter():
                title = f'{ch.name} [COLOR gray][{ch.code or ""}][/COLOR]'
                if ch.code:
                    kdir.play(title, call(self.station, ch.code, '.pvr'), image=ch.img)
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
            # log(f'tv_tree({to_get})...')
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
            #             ' video_format={video_format_len}, videoFormatMimes={videoFormatMimes_len}, title={title!r}'),
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
                self._item(kdir, items[0], title=title)
                log(title)

    @entry(path='/replay', title=L(30115, 'Archive'))
    def replay_list(self):
        with self.directory() as kdir:
            for item in self.site.stations():
                image = self._item_image(item, preferred='image_square')
                name, code = item['name'], item.get('code', '')
                kdir.menu(name, call(self.replay_channel, code), image=image,
                          menu=[(L(30123, 'Live TV'), self.cmd.PlayMedia(call(self.station, code)))])

    @entry(path='/replay/<code>')
    def replay_channel(self, code):
        now = datetime.now()
        ar_date = [now - timedelta(days=n) for n in range(7)]
        with self.directory() as kdir:
            for date in ar_date:
                label = f'{date:%Y-%m-%d}'
                menu = []
                if self.settings.developing or self.settings.debugging:
                    menu.append(('!!!', self.exception))
                menu.append((L(30123, 'Live TV'), self.cmd.PlayMedia(call(self.station, code))))
                kdir.menu(label, call(self.replay_date, code=code, date=f'{date:%Y%m%d}'), menu=menu)

    @entry(path='/replay/<code>/<date>')
    def replay_date(self, code, date, *, future=False):
        now_msec = int(datetime.now().timestamp() * 1000)  # TODO handle timezone
        with self.directory() as kdir:
            for prog in self.site.station_full_epg(code, date):
                archive = prog['date_start'] < now_msec
                if archive or future:
                    pid = prog['record_id']
                    eprog = prog.get('program', {})
                    img = self._item_image(eprog, eprog.get('cycle'))
                    title, label2 = self.fmt('{prog.times} {prog.title}', prog=prog)
                    info = {
                        'outline': prog.outline,
                        'plotoutline': f'[B]{prog.title}[/B][CR]{prog.outline}',
                        'plot': prog.descr,
                    }
                    if archive:
                        kdir.play(title, call(self.play_program, code=code, prog=pid), image=img, info=info,
                                  label2=label2)
                    else:
                        title = f'[I]{title}[/I]'
                        kdir.item(title, self.no_operation, image=img, info=info, label2=label2)

    def _epg_item(self, kdir, item, *, code=None, now=None):
        if now is None:
            now = datetime.now()
        if type(item) is ChannelProgram:
            prog = item
        else:
            prog = ChannelProgram(item)
        if code is None:
            code = prog['station_code']
        pid = prog['record_id']
        eprog = prog.get('program', {})
        img = self._item_image(eprog, eprog.get('cycle'))
        if now.date() == prog.start.date():
            title = f'[{prog.times}] {prog.title}'
            short = f'[B]{prog.title}[/B][CR]{prog.outline}'
        else:
            title = f'[{prog.start:%Y-%m-%d}] {prog.title}'
            short = f'[B]{prog.title}[/B][CR]{prog.start:%Y-%m-%d %H:%M} - {prog.end:%H:%M}[CR]{prog.outline}'
        info = {
            'outline': prog.outline,
            'plotoutline': short,
            'plot': prog.descr,
        }
        kdir.play(title, call(self.play_program, code=code, prog=pid), image=img, info=info)

    @entry(path='/station-program/<code>/<date>')
    def station_program(self, code, date, *, future=True):
        return self.replay_date(code, date, future=future)

    def play_hbb(self, id: PathArg, code: PathArg = ''):
        ...

    def play_program(self, code, prog):
        streams, mimetype = self.site.station_streams(code, prog)
        if streams:
            stream = self.get_stream_of_type(streams, mimetype=mimetype)
            self._play(stream)

    def station(self, code: PathArg, pvr_suffix):
        date = datetime.today()
        program = self.site.jget('https://tvpstream.tvp.pl/api/tvp-stream/program-tv/index',
                                 params={'station_code': code, 'date': date}).get('data')

        if program:
            now = int(datetime.now().timestamp() * 1000)
            begin_ts, end_ts = [(item['date_start'], item['date_end']) for item in program if
                                item['date_start'] <= now <= item['date_end']][0]

            begin_date = datetime.fromtimestamp(int(begin_ts) // 1000) - timedelta(hours=2)
            end_date = datetime.fromtimestamp(int(end_ts) // 1000) - timedelta(hours=2)

            begin_date = begin_date.strftime('%Y%m%dT%H%M%S')
            end_date = end_date.strftime('%Y%m%dT%H%M%S')

            begin_tag = begin_date
            end_tag = end_date
        else:
            begin_tag = None
            end_tag = None

        data = self.site.jget('https://tvpstream.tvp.pl/api/tvp-stream/stream/data', params={'station_code': code}).get(
            'data')
        if data:
            redir = self.site.jget(data['stream_url'])
            formats = redir.get('formats')

            live = redir.get('live')
            if live:
                live_tag = 'true'
            else:
                live_tag = 'false'

            timeshift = redir.get('timeShift')
            if timeshift:
                timeshift_tag = 'true'
            else:
                timeshift_tag = 'false'

            mimetype = redir.get('mimeType')

            stream = self.get_stream_of_type(formats or (), begin=begin_tag, end=end_tag, live=live_tag,
                                             timeshift=timeshift_tag, mimetype=mimetype)
            self._play(stream)

    def _play(self, stream):
        log(f'PLAY {stream!r}')
        from inputstreamhelper import Helper
        if stream.proto:
            is_helper = Helper(stream.proto)
            if is_helper.check_inputstream():
                play_item = xbmcgui.ListItem(path=stream.url)
                if stream.mime is not None:
                    play_item.setMimeType(stream.mime)
                play_item.setContentLookup(False)
                play_item.setProperty('inputstream', is_helper.inputstream_addon)
                play_item.setProperty("IsPlayable", "true")
                play_item.setProperty('inputstream.adaptive.manifest_type', stream.proto)
                play_item.setProperty('inputstream.adaptive.license_type', 'com.widevine.alpha')
                play_item.setProperty('inputstream.adaptive.manifest_update_parameter', 'full')
                play_item.setProperty('inputstream.adaptive.stream_headers', 'Referer: https://vod.tvp.pl/&User-Agent='+quote(UA))
                play_item.setProperty('StartOffset', '60.0')
                if KODI_VERSION >= 20:
                    play_item.setProperty('inputstream.adaptive.stream_selection_type', 'manual-osd')
                if 'live=true' not in stream.url:
                    play_item.setProperty('inputstream.adaptive.play_timeshift_buffer', 'true')
                xbmcplugin.setResolvedUrl(handle=self.handle, succeeded=True, listitem=play_item)
        else:
            play_item = xbmcgui.ListItem(path=stream.url)
            if stream.mime is not None:
                play_item.setMimeType(stream.mime)
            play_item.setContentLookup(False)
            play_item.setProperty("IsPlayable", "true")
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
            for item in self.site.transmissions_items(id, filter_dict=CurrentAndFuture):
                # Only current and future
                end = self._item_end_time(item)
                if end:
                    # log(f'now={now}, @now={local_now}, end={end} ({end + self.tz_offset}), live={item.get(
                    # "is_live")}', title='===TIME===')
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

    @staticmethod
    def _item_image(*items, preferred=None, default=None):
        for item in items:
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
            return default

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
        label2 = None
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
                time = start + self.tz_offset
                # date-time format condition
                if single_day or start.date() == now.date() or start + timedelta(hours=6) < now:
                    # the same day or next 6h: only HH:MM
                    day_cond = 'one_day'
                else:
                    # more then 6h: yyyy.mm.dd HH:MM
                    day_cond = 'another_day'
                # date-time style condition
                if future:
                    time_cond = 'future'
                elif end < now:  # transmisja powinna być zakończona, do 5min
                    time_cond = 'finished'
                else:
                    time_cond = 'current'
                # TIME - TITLE,  !? - format cond name, !$ - style cond name
                title, label2 = self.fmt('{trans.time:!?day_cond!$time_cond} {trans.title}',
                                         trans={'title': title, 'time': time},
                                         time_cond=time_cond, day_cond=day_cond)
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
        if self.settings.developing:
            if self.settings.debugging:
                menu.append(('!!!', self.exception))
            else:
                menu.append((f'!!! {iid}', self.exception))
        if self.settings.debugging:
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
        kwargs = dict(image=image, descr=descr, custom=custom, position=position, menu=menu, style=style,
                      label2=label2)
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
                # resp = self.site.txtget(url, allow_redirects=True) r = re.search(r'a="(?P<a>\d+)",s="(?P<s>\d+)",
                # l="(?P<l>\d+)",c="(?P<c>[^"]*)"', resp) if r: S, L = r.group('s', 'l') url =
                # f'https://kmc.europarltv.europa.eu/p/{S}/sp/{S}00/embedIframeJs/uiconf_id/{L}/partner_id/{S}'
                xbmcgui.Dialog().notification('[B]TVP[/B]', 'Embedded player jest nieobsługiwany',
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
        # !!! if data.get('type') == 'virtual_channel' and 'live_video_id' in data:
        # !!!     id = data['live_video_id']
        start = self._item_start_time(data)
        end = self._item_end_time(data)
        subt = ''
        if start:
            now = datetime.utcnow()
            if not end:
                try:
                    end = start + timedelta(seconds=data['duration'])
                except KeyError:
                    end = now + timedelta(days=1)
            # if not start < now < end:  # sport only current
            if not data.get('paymethod') and start > now:  # future
                xbmcgui.Dialog().notification('[B]TVP[/B]', 'Transmisja aktualnie niedostępna',
                                              xbmcgui.NOTIFICATION_INFO)
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
                    xbmcgui.Dialog().notification('[B]TVP[/B]', L(30158, '[ABO zone] Information'),
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
                    xbmcgui.Dialog().notification(L(30160, '[B]Error[/B]'), L(30159, '[ABO zone] No authorization'),
                                                  xbmcgui.NOTIFICATION_INFO, 8000, False)
                else:
                    for d in resp['data']:
                        if 'id' in d:
                            if d['id'] == id:
                                if Ttml2SsaAddon is not None:
                                    subt = self.subt_gen_abo(d)
                                if d['is_drm'] is True:  # DRM
                                    url_stream = re.findall('fileDash\': \'([^\']+?)\'', str(resp))[0]
                                    lic_url = re.findall('proxyWidevine\': \'([^\']+?)\'', str(resp))[0]
                                    # print(url_stream)
                                    # print(licUrl)
                                    if url_stream and lic_url:
                                        import inputstreamhelper
                                        protocol = 'mpd'
                                        drm = 'com.widevine.alpha'
                                        is_helper = inputstreamhelper.Helper(protocol, drm=drm)
                                        if is_helper.check_inputstream():
                                            play_item = xbmcgui.ListItem(path=url_stream)
                                            if Ttml2SsaAddon is not None:
                                                play_item.setSubtitles(subt)
                                            play_item.setProperty("inputstream", is_helper.inputstream_addon)
                                            play_item.setProperty("inputstream.adaptive.manifest_type", protocol)
                                            play_item.setContentLookup(False)
                                            play_item.setProperty("inputstream.adaptive.license_type", drm)
                                            play_item.setProperty("inputstream.adaptive.license_key",
                                                                  lic_url + '||R{SSM}|')
                                            xbmcplugin.setResolvedUrl(self.handle, True, listitem=play_item)

                                            return
                                else:  # non-DRM
                                    streams = d['formats']
                                    stream = sorted(streams, key=lambda d: (int(d['totalBitrate'])), reverse=True)[0]

                                    if 'material_niedostepny' not in stream['url']:
                                        play_item = xbmcgui.ListItem(path=stream['url'])
                                        play_item.setProperty('IsPlayable', 'true')
                                        if Ttml2SsaAddon is not None:
                                            play_item.setSubtitles(subt)
                                        xbmcplugin.setResolvedUrl(self.handle, True, listitem=play_item)
                                    else:
                                        xbmcgui.Dialog().notification('[B]TVP[/B]', L(30157, 'Stream not available'),
                                                                      xbmcgui.NOTIFICATION_INFO, 3000, False)
                                        self.play_failed()
        else:  # free
            log(f'free video: {id}', title='TVP')
            stream = Stream(stream_url, '', '')
            if 'material_niedostepny' not in stream.url:
                if 'formats' in resp:
                    stream = self.get_stream_of_type(resp['formats'], mimetype=resp['mimeType'])
                    if stream_url:
                        if (stream.mime == 'application/x-mpegurl' and 'end' in stream.url.query
                                and '.m3u8' in str(stream.url) and not self.site.head(stream.url).ok):
                            log(f'Remove `end` from {url!r}')
                            url = stream.url
                            url = url.with_query([(k, v) for k, v in url.query.items() if k != 'end'])
                            stream = stream._replace(url=url)
                        return self._play(stream)

                subt = self.subt_gen_free(id)
                if stream:
                    return self._play(stream)

            else:
                xbmcgui.Dialog().notification('[B]TVP[/B]', L(30157, 'Stream not available'), xbmcgui.NOTIFICATION_INFO, 3000, False)
                self.play_failed()

    def subt_gen_abo(self, d):
        """Tablica z linkami do plików z napisami (format .ssa)."""
        subt = []
        if 'subtitles' in d:
            if d['subtitles']:
                path: Path = self.profile_path / 'temp'
                path.mkdir(parents=True, exist_ok=True)
                for n, it in enumerate(d['subtitles']):
                    url_subt = it['src']
                    resp = self.site.get(url_subt)
                    ttml = Ttml2SsaAddon()
                    ttml.parse_ttml_from_string(resp.text)
                    ttml.write2file(path / f'subt_{n + 1:02d}.ssa')
                    subt.append(path / f'subt_{n + 1:02d}.ssa')
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
                url_subt = url.join(d['url'])
                ttml = Ttml2SsaAddon()
                ttml.parse_ttml_from_string(self.site.get(url_subt).text)
                ttml.write2file(path / f'subt_{n + 1:02d}.ssa')
                subt.append(path / f'subt_{n + 1:02d}.ssa')
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
    def search_bestresults(self, query):
        def details(con, item):
            itype = item.get('type')
            if itype == 'OCCURRENCE':
                return con.occurrence(item['id'])
            else:
                return con.details(item['id'])

        now = datetime.now()
        url = f'https://sport.tvp.pl/api/tvp-stream/search?query={query}&scope=bestresults&page=1&limit=&device=android'
        with self.directory() as kdir:
            items = self.site.jget(url).get('data', {}).get('occurrenceitem', ())
            with self.site.concurrent() as con:
                indexes = [details(con, item) for item in items if 'id' in item]
            # items = [{**con[i], **{'FOUND': found}} for i, found in zip(indexes, items)]
            for index, found in zip(indexes, items):
                itype = found.get('type')
                if itype == 'OCCURRENCE':
                    self._epg_item(kdir, con[index]['data'], now=now)
                else:
                    item = con[index]
                    item['FOUND'] = found
                    cycle = found.get('program', {}).get('cycle', {})
                    if cycle and cycle.get('title'):
                        item['SERIES'] = {
                            'id': item['parents'][0],
                            'title': cycle['title'],
                            'image_logo': cycle.get('image_logo'),
                        }
                    self._item(kdir, item)

    def vod_search_folder(self, query):
        sep = True
        with self.directory() as kdir:
            page = self.site.txtget('https://vod.tvp.pl/szukaj', params={'query': query})
            # log(f'VS: page.len={len(page)!r}')
            for jsdata in dom_select(page, 'div.serachContent div.item.js-hover(data-hover)'):  # "serachContent" (sic!)
                item = json.loads(unescape(jsdata))
                # log(f'VS: {item!r}')
                sid = item['myListId']  # seris link
                title = item['title']
                episode = item.get('episodeCount')
                if episode:
                    title = f'{title}, {episode}'
                    if sep:
                        sep = False
                        kdir.separator('Odcinki')
                    sid = item.get('episodeLink', sid).rpartition(',')[2]
                    kdir.play(title, call(self.video, sid), image=item['image'], descr=item.get('description'))
                else:
                    kdir.menu(title, call(self.listing, sid), image=item['image'], descr=item.get('description'))

    def bitrate_selector(streams):
        selector = []

        streams = sorted(streams, key=lambda d: (-int(d['totalBitrate'])), reverse=True)

        for stream in streams:
            bitrate = int(stream['totalBitrate'] / 1000)
            mimetype = stream['mimeType']

            if bitrate >= 4000:
                res = f'1080p, Stream type: {mimetype}'
            elif bitrate >= 1500:
                res = f'720p, Stream type: {mimetype}'
            elif bitrate >= 1200:
                res = f'540p, Stream type: {mimetype}'
            else:
                res = f'480p, Stream type: {mimetype}'

            selector.append(res)

        ret = xbmcgui.Dialog().select('Select stream', selector)
        return streams[ret]

    @staticmethod
    def iter_stream_of_type(streams, *, begin, end, live, timeshift, mimetype):
        settings = Settings()
        if settings.bitrate_selector == 0:
            stream = TvpPlugin.bitrate_selector(streams)

        # highest quality
        elif settings.bitrate_selector >= 1:
            streams_ = [d for d in streams if mimetype == d['mimeType']]

            if not streams_:
                streams_ = streams

            # 1080p
            elif settings.bitrate_selector == 2:
                stream = sorted(streams_, key=lambda d: (-int([d['totalBitrate'] for d in streams if d['totalBitrate'] / 1000 >= 4000][0])), reverse=True)[-1]

            # 720p
            elif settings.bitrate_selector == 3:
                stream = sorted(streams_, key=lambda d: (-int([d['totalBitrate'] for d in streams if d['totalBitrate'] / 1000 >= 1500][0])), reverse=True)[-1]

            # 560p
            elif settings.bitrate_selector == 4:
                stream = sorted(streams_, key=lambda d: (-int([d['totalBitrate'] for d in streams if d['totalBitrate'] / 1000 >= 1200][0])), reverse=True)[-1]

            # 480p
            elif settings.bitrate_selector == 5:
                stream = sorted(streams_, key=lambda d: (-int([d['totalBitrate'] for d in streams if d['totalBitrate'] / 1000 >= 0][0])), reverse=True)[-1]

            else:
                stream = sorted(streams_, key=lambda d: (-int(d['totalBitrate'])), reverse=True)[-1]

        mimetype = stream['mimeType']

        if mimetype == 'application/dash+xml' or mimetype == 'application/xml+dash':
            protocol = 'mpd'
        elif mimetype == 'application/vnd.ms-ss':
            protocol = 'hls'
        elif mimetype == 'application/vnd.apple.mpegurl':
            protocol = 'hls'
        elif mimetype == 'application/x-mpegurl':
            protocol = 'hls'
        else:
            protocol = ''

        if 'material_niedostepny' not in stream['url']:
            url = stream['url']
            if 'ism/manifest' in url:
                url = url.replace('/manifest', '/video.m3u8')

            params = {}
            if begin:
                tag = '?begin='
                if 'begin=' in url:
                    begin = re.sub(r'^\?begin=\d+T\d+', tag + begin, url)

            if end and 'end=' not in url:
                if 'begin=' in url:
                    params.update({'end': end})

            if live and 'live=' not in url:
                params.update({'live': live})

            if timeshift and 'timeshift=' not in url:
                params.update({'timeshift': timeshift})

            parsed_url = urlparse(url)
            parsed_query = parse_qs(parsed_url.query)
            if parsed_query:
                start_tag = '&'
            else:
                start_tag = '?'

            url_ = URL(url + start_tag + urlencode(params))

            return Stream(url=url_, proto=protocol, mime=mimetype)

        else:
            xbmcgui.Dialog().notification('[B]TVP[/B]', L(30157, 'Stream not available'), xbmcgui.NOTIFICATION_INFO,
                                          3000, False)
            return

    @staticmethod
    def get_stream_of_type(streams, *, begin=None, end=None, live='', timeshift='', mimetype=None):
        stream = TvpPlugin.iter_stream_of_type(streams, begin=begin, end=end, live=live, timeshift=timeshift, mimetype=mimetype)
        return stream

    def exception(self):
        raise RuntimeError()

    # Generator m3u – do zaorania
    # TODO: make generator in the libka
    def build_m3u(self):
        path_m3u = self.settings.m3u_folder
        file_name = self.settings.m3u_filename

        if not file_name or not path_m3u:
            xbmcgui.Dialog().notification('[B]TVP[/B]', L(30132, 'Set filename and destination directory'),
                                          xbmcgui.NOTIFICATION_ERROR)
            return

        xbmcgui.Dialog().notification('[B]TVP[/B]', L(30134, 'Generate playlist'), xbmcgui.NOTIFICATION_INFO)
        data = '#EXTM3U\n'

        for ch in self.channel_iter_stations():
            url = self.mkurl(self.station, code=ch.code)
            data += f'#EXTINF:0 tvg-id="{ch.name}" tvg-logo="{ch.image}" group-title="TVP",{ch.name}\n{url}\n'

        try:
            f = xbmcvfs.File(path_m3u + file_name, 'w')
            f.write(data)
        finally:
            f.close()
        xbmcgui.Dialog().notification('[B]TVP[/B]', L(30135, 'Playlist M3U generated'), xbmcgui.NOTIFICATION_INFO)


# DEBUG ONLY
import sys  # noqa

log(f'TVP: {sys.argv}')

# Create and run plugin.
TvpPlugin().run()
