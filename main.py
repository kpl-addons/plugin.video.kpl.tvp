
from libka import L, Plugin, Site
from libka import call, PathArg, entry
from libka.logs import log
from libka.url import URL
from libka.path import Path
from libka.menu import Menu, MenuItems
from libka.utils import html_json, html_json_iter
from libka.format import safefmt
# from pdom import select as dom_select
import json
from collections.abc import Mapping
from collections import namedtuple
from html import unescape
from datetime import datetime, timedelta
import re
import xbmcgui  # dialogs
import xbmcplugin  # setResolvedUrl
try:
    from ttml2ssa import Ttml2SsaAddon
except ModuleNotFoundError:
    Ttml2SsaAddon = None  # DEBUG only


# XXX
# Na razie wszystko jest w jednym pliku, bo łatwiej odświeżać w kodi.
# Potem poszczególne klasy wylądują w resources/lib/
# XXX

# TODO:
# TVP VOD - https://vod.tvp.pl/
# TVP SPORT Magazyny - https://sport.tvp.pl/magazyny
# TVP SPORT Retransmisje - https://sport.tvp.pl/retransmisje
# TVP SPORT Wideo(?) - https://sport.tvp.pl/wideo
# TVP INFO - https://www.tvp.info/nasze-programy


UNLIMITED = object()


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

ChannelInfo = namedtuple('ChannelInfo', 'code name img id')


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

    def listing(self, parent_id, *, dump='json', direct=True, **kwargs):
        count = kwargs.pop('count', self.count)
        if count is None or count is UNLIMITED:
            count = ''
        return self.jget('/shared/listing.php',
                         params={'dump': dump, 'direct': direct, 'count': count, 'parent_id': parent_id, **kwargs})

    def listing_items(self, parent_id, *, dump='json', direct=True, **kwargs):
        data = self.listing(parent_id, dump=dump, direct=direct, **kwargs)
        for item in data.get('items') or ():
            yield item

    # Dicts `filter` and `order` could be in arguments because they are read-only.
    def transmissions(self, parent_id, dump='json', direct=False, type='epg_item',
                      filter={'is_live': True}, order={'release_date_long': -1}, **kwargs):
        return self.listing(parent_id, dump=dump, direct=direct, type=type, filter=filter, order=order, **kwargs)

    # Dicts `filter` and `order` could be in arguments because they are read-only.
    def transmissions_items(self, parent_id, dump='json', direct=False, type='epg_item',
                            filter={'is_live': True}, order={'release_date_long': -1}, **kwargs):
        data = self.transmissions(parent_id, dump=dump, direct=direct, type=type, filter=filter, order=order, **kwargs)
        # reverse reversed ('release_date_long': -1) list
        for item in reversed(data.get('items') or ()):
            yield item

    def details(self, object_id, *, dump='json', **kwargs):
        return self.jget('/shared/details.php',
                         params={'dump': dump, 'object_id': object_id, **kwargs})


class TvpPlugin(Plugin):
    """tvp.pl plugin."""

    MENU = Menu(order_key='title', items=[
        Menu(title='Tests', items=[
            Menu(title=L('API Tree'), id=2),
            Menu(title='VoD', id=1785454),
            Menu(title='Retransmisje', id=48583081),
            Menu(title="m1992's TV", id=68970),
            Menu(call='tv_hbb'),
            Menu(call='tv_stations'),
            Menu(call='tv_html'),
            Menu(call='tv_tree'),
        ]),
        Menu(call='tv'),
        MenuItems(id=1785454, type='directory_series', order={2: 'programy', 1: 'seriale', -1: 'teatr*'}),
        # Menu(title='Rekonstrucja cyfrowa', id=35470692),  --- jest już powyższym w MenuItems(1785454)
        Menu(title='Sport', items=[
            Menu(title='Submenu test', items=[
                Menu(title='Transmisje', call='sport'),
                Menu(title='Retransmisje', id=48583081),
            ]),
            Menu(title='Transmisje', call='sport'),
            Menu(title='Retransmisje', id=48583081),
            Menu(title='Magazyny', id=548368),
            Menu(title='Wideo', id=432801),
        ]),
        Menu(title='Parlament', items=[
            Menu(title='Transmisje', call=call('transmissions', 4422078)),
            Menu(title='Retransmisje', id=4433578),
            Menu(title='Programy EuroparlTV', id=4615555),
        ]),
        Menu(title='TVP Info', id=191888),
        Menu(call='search'),
    ])

    epg_url = 'http://www.tvp.pl/shared/programtv-listing.php?station_code={code}&count=100&filter=[]&template=json%2Fprogram_tv%2Fpartial%2Foccurrences-full.html&today_from_midnight=1&date=2022-04-25'

    def __init__(self):
        super().__init__()
        self.site = TvpSite()

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

    def listing(self, id: PathArg[int], type=None):
        """Use api.v3.tvp.pl JSON listing."""
        # TODO:  determine `view`
        with self.site.concurrent() as con:
            con.a.data.listing(id)
            con.a.details.details(id)
        data = con.a.data
        details = con.a.details

        with self.directory(view='movies') as kdir:
            kdir.item(f'=== {id}', call(self.enter_listing, id=id))  # XXX DEBUG
            items = data.get('items') or ()
            # if items:
            #     parents = items[0]['parents'][1:]
            #     if parents:
            #         kdir.menu('^^^', call(self.listing, id=parents[0]))  # XXX DEBUG
            if len(items) == 1 and items[0].get('object_type') == 'directory_video' and items[0]['title'] == 'wideo':
                # Oszukany katalog sezonu, pokaż id razu odcinki.
                data = self.site.listing(items[0]['asset_id'])
                items = data.get('items') or ()

            # Analiza szcegółów, w tym dokładnych opisów i danych video
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
                self._item(kdir, item, debug=True)

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

    @entry(title=L('TV'))
    def tv(self):
        """TV channel list."""
        # Regionalne: 38345166 → vortal → virtual_channel → live_video_id
        with self.directory() as kdir:
            for item in self.site.jget('https://tvpstream.tvp.pl/api/tvp-stream/program-tv/stations')['data']:
                image = self._item_image(item, preferred='image_square')
                name, code = item['name'], item.get('code', '')
                kdir.play(f'{name} [COLOR gray][{code}][/COLOR]', call(self.play_tvp_stream, code), image=image)

    @entry(title=L('TV (HBB)'))
    def tv_hbb(self):
        """TV channel list."""
        with self.directory() as kdir:
            for ch in self.channel_iter():
                title = f'{ch.name} [COLOR gray][{ch.code or ""}][/COLOR]'
                if ch.code:
                    kdir.play(title, call(self.play_tvp_stream, ch.code), image=ch.img)
                else:
                    title += f' [COLOR gray]{ch.id}[/COLOR]'
                    kdir.play(title, call(self.video, ch.id), image=ch.img)

    @entry(title=L('TV (tv-stations)'))
    def tv_stations(self):
        """TV channel list."""
        with self.directory() as kdir:
            for item in self.site.jget('https://tvpstream.tvp.pl/api/tvp-stream/program-tv/stations')['data']:
                image = self._item_image(item, preferred='image_square')
                name, code = item['name'], item.get('code', '')
                kdir.play(f'{name} [COLOR gray][{code}][/COLOR]', call(self.play_tvp_stream, code), image=image)

    @entry(title=L('TV (html)'))
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
                kdir.play(f'{name} [COLOR gray][{code}][/COLOR]{extra}', call(self.play_tvp_stream, code), image=img)

    @entry(title=L('TV (drzewo)'))
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

    # @entry(title=L('Live'), path='parlament/live')
    # @entry(title=L('Live'))
    # def parlament_live(self, id=4422078):
    #     self.transmissions(id)

    def play_hbb(self, id: PathArg, code: PathArg = ''):
        ...

    def play_tvp_stream(self, code):
        data = self.site.jget('https://tvpstream.tvp.pl/api/tvp-stream/stream/data',
                              params={'station_code': code}).get('data')
        if data:
            stream = self.get_stream_of_type(self.site.jget(data['stream_url']).get('formats') or ())
            self._play(stream)

    def _play(self, stream):
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

    def transmissions(self, id: PathArg):
        """Live transmistions (sport: 13010508, parlament: 4422078)."""
        now = datetime.utcnow()
        with self.directory() as kdir:
            # Reverse reversed order - get from current to future.
            for item in self.site.transmissions_items(id):
                # Only current and future
                end = item.get('broadcast_end_date_long', 0)
                if end:
                    end = datetime.utcfromtimestamp(end / 1000)
                    if end < now:
                        continue  # skip past tranmison
                self._item(kdir, item)

    def sport(self, id=13010508):
        """Sport transmistion (13010508)."""
        self.transmissions(id)

    def _item_image(self, item, *, preferred=None):
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

    def _item(self, kdir, item, *, custom=None, title=None, debug=True):
        itype = item.get('object_type')
        iid = item.get('asset_id')
        playable = item.get('playlable')
        details = item.get('DETAILS', {})
        # format title
        if title is None:
            title = item.get('title', item.get('name', f'#{iid}'))
            if item.get('website_title'):
                title = f'{item["website_title"]}: {title}'
            elif item.get('title_root'):
                title = item['title_root']
        if title[:1].islower():
            title = title[0].upper() + title[1:]
        title = f'{title} [COLOR gray]({itype or "???"})[/COLOR]'  # XXX DEBUG
        # broadcast time
        start = item.get('release_date_long', item.get('broadcast_start_long', 0))
        end = item.get('broadcast_end_date_long', 0)
        if start and end:
            start = datetime.utcfromtimestamp(start / 1000)
            end = datetime.utcfromtimestamp(end / 1000)
            now = datetime.utcnow()
            if start > now or 1:
                title += f' [{start:%H:%M %d.%m.%Y}]'
        # image
        image = self._item_image(item)
        # description
        descr = item.get('lead_root') or item.get('description_root')
        if 'commentator' in item:
            descr += '\n\n[B]Komentarz[/B]\n' + item['commentator']
        for cue in details.get('cue_card') or ():
            for par in cue.get('text_paragraph_standard') or ():
                descr += '[CR]{}'.format(par.get('text', '').replace(r'\n', '[CR]'))
        descr = remove_tags(descr)
        # menu
        menu = []
        # raise KeyError('a')
        if debug:
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
        # item
        position = 'top' if itype == 'directory_toplist' else None
        kwargs = dict(image=image, descr=descr, custom=custom, position=position, menu=menu)
        if itype in ('video', 'epg_item'):
            # if 'virtual_channel_id' in item:
            #     iid = item['virtual_channel_id']
            # elif
            if 'video_id' in item:
                iid = item['video_id']
            kdir.play(title, call(self.video, id=iid), **kwargs)
        else:
            if item.get('VIDEOS'):
                if len(item['VIDEOS']) == 1:
                    vid = item['VIDEOS'][0]
                    kdir.play(title, call(self.video, id=vid), **kwargs)
                    return
                iid = item.get('VIDEO_DIRECTORY', iid)
            kdir.menu(title, call(self.listing, id=iid), **kwargs)

    def video(self, id: PathArg[int]):
        """Play video – PlayTVPInfo by mtr81."""
        # TODO: cleanup
        data = self.site.details(id)
        log(f"Video: {id}, type={data.get('type')}, live_video_id={data.get('live_video_id')},"
            f" video_id={data.get('video_id')}", title='TVP')
        if data.get('type') == 'virtual_channel' and 'live_video_id' in data:
            id = data['live_video_id']
        # 'type': 'virtual_channel',
        # 'object_type': 'virtual_channel',
        # 'parents': [38533322, 38345166, 38345015, 2],
        # 'signature': 'TVP3 Wrocław',
        # 'title': 'EPG - TVP Wroc',
        # 'vortal_id': 38533322,
        # 'live_video_id': 50841981,
        start = data.get('release_date_long', data.get('broadcast_start_long', 0))
        end = data.get('broadcast_end_date_long', 0)
        if start:
            now = datetime.utcnow()
            start = datetime.utcfromtimestamp(start / 1000)
            if end:
                end = datetime.utcfromtimestamp(end / 1000)
            else:
                try:
                    end = start + timedelta(seconds=data['duration'])
                except KeyError:
                    end = now + timedelta(days=1)
            # if not start < now < end:  # sport only current
            if start > now:  # future
                xbmcgui.Dialog().notification('TVP', 'Transmisja niedostępna teraz', xbmcgui.NOTIFICATION_INFO)
                xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem())
                log(f'Video {id} in future: {start} > {now}', title='TVP')
                return

        if 'video_id' in data:
            id = data['video_id']
        site = Site()
        url = f'https://www.tvp.pl/shared/cdn/tokenizer_v2.php?object_id={id}&sdt_version=1&end='
        resp = site.jget(url)
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
                'username': self.settings.getSetting('email'),
                'client_secret': 'Qao*kN$t10',
                'grant_type': 'password',
                'password': self.settings.getSetting('password')
            }
            resp = site.jpost('http://www.tvp.pl/sess/oauth/oauth/access_token.php', headers=hea, data=data)
            token = ''
            log(f'TVP oauth resp: {resp!r}')
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
                resp = site.jpost(f'https://apivod.tvp.pl/tv/v2/video/{id}/default/default?device=android',
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
            if 'formats' in resp:
                for f in resp['formats']:
                    if f['mimeType'] == 'application/x-mpegurl':
                        stream_url = f['url']
                        break
                else:
                    xbmcgui.Dialog().notification('[B]Błąd[/B]', 'Brak strumienia do odtworzenia.',
                                                  xbmcgui.NOTIFICATION_INFO, 3000, False)
                    return
            subt = self.subt_gen_free(id)
            play_item = xbmcgui.ListItem(path=stream_url)
            play_item.setProperty('IsPlayable', 'true')
            play_item.setSubtitles(subt)
            log(f'PLAY: handle={self.handle!r}, url={stream_url!r}', title='TVP')
            xbmcplugin.setResolvedUrl(self.handle, True, listitem=play_item)

    def subt_gen_ABO(self, d):
        """Tablica z linkami do plików z napisami (format .ssa)."""
        subt = []
        if 'subtitles' in d:
            if d['subtitles']:
                path = self.profile_path / 'temp'
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
        respJSON = re.findall(r'_\((.*)\)', resp, re.DOTALL)[0]
        try:
            data = json.loads(respJSON)['content']
        except Exception as exc:
            log.warning(f'Subtiles JSON failed {exc} on {respJSON!r}', title='TVP')
            return []
        subt = []
        path = self.profile_path / 'temp'
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

    def channel_iter(self):
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
            yield ChannelInfo(code, name, img, ch_id)

    @staticmethod
    def get_stream_of_type(streams):
        mime_types = {
            'application/vnd.ms-ss': StreamType('ism', 'application/vnd.ms-ss'),
            'video/mp4':             StreamType('hls', 'application/x-mpegURL'),
            'video/mp2t':            StreamType('hls', 'application/x-mpegURL'),
            'application/dash+xml':  StreamType('mpd', 'application/xml+dash'),
            'application/x-mpegurl': StreamType('hls', 'application/x-mpegURL'),
        }

        for st in streams:
            for prio, mime in enumerate(mime_types):
                if st['mimeType'] == mime:
                    st['priority'] = prio

        streams = sorted(streams, key=lambda d: (-int(d['totalBitrate']), d['priority']), reverse=True)
        for st in streams:
            if 'material_niedostepny' not in st['url']:
                for mime, stype in mime_types.items():
                    if st['mimeType'] == mime:
                        url = URL(st['url']) % {'end': ''}
                        return Stream(url=url, proto=stype.proto, mime=stype.mime)


# DEBUG ONLY
import sys  # noqa
log(f'\033[1mTVP\033[0m: \033[93mENTER\033[0m: {sys.argv}')

# Create and run plugin.
TvpPlugin().run()


# Full GraphQL TV list query
"""
query ($categoryId: String) {
    getLandingPageVideos(categoryId: $categoryId) {
        type
        title
        elements {
            id
            title
            subtitle
            type
            img {
                hbbtv
                image
                website_holder_16x9
                video_holder_16x9
                __typename
            }
            broadcast_start_ts
            broadcast_end_ts
            sportType
            label {
                type
                text
                __typename
            }
            stats
            {
                video_count
                __typename
            }
            __typename
       }
        __typename
    }

    getStationsForMainpage {
        items {
            id
            name
            code
            image_square {
                url
                __typename
            }
            background_color
            isNativeChanel
            __typename
        }
        __typename
    }
}
"""
