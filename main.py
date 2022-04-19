
from libka import L, Plugin, Site
from libka import call, PathArg, entry
from libka.logs import log
from libka.url import URL
from libka.path import Path
from pdom import select as dom_select
import json
from fnmatch import fnmatch
from collections import namedtuple
from html import unescape
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
    fname: str = image['file_name']
    name, _, ext = fname.rpartition('.')
    width = image.get('width', 1280)
    return URL(f'http://s.v3.tvp.pl/images/{name[:1]}/{name[1:2]}/{name[2:3]}/uid_{name}_width_{width}_gs_0.{ext}')


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
            # if 'width' in image and 'height' in image:
            #     image = re.sub(r'width_\d+', 'width_1280', image)
            #     image = re.sub(r'height_\d+', 'height_760', image)
            return Info(data, type=data['type'], url=url, title=data['title'], image=image,
                        descr=data.get('description'), series=(eLink != sLink),
                        linkid=url.path.rpartition(',')[2])
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            log.warning(f'Can not parse video info {exc} from: {data!r}')
            return None


class TvpVodSite(Site):
    """vod.tvp.pl site."""


Menu = namedtuple('Menu', 'id title call items', defaults=(None, None, None, None))
MenuItems = namedtuple('MenuItems', 'id type order', defaults=(None, None))


class TvpPlugin(Plugin):
    """tvp.pl plugin."""

    MENU = Menu(items=[
        Menu(call='tv'),
        MenuItems(id=1785454, type='directory_series', order={2: 'programy', 1: 'seriale'}),
        Menu(title='Sport', items=[
            Menu(title='Retransmisje', id=48583081),
        ]),
        Menu(call='search'),
    ])

    def __init__(self):
        super().__init__()
        # self.vod = Site(base='https://vod.tvp.pl')
        self.site = Site(base='http://www.api.v3.tvp.pl/shared/listing.php?dump=json&direct=true')
        self.limit = 1000

    def home(self):
        with self.directory() as kdir:
            kdir.menu(L('Tests'), self.tests)
            self._menu(kdir)
        # 48583081 - retransmisje
        # self.listing(41055208)  # sezon
        # self.listing(1649941)  # seriale (mtr81)

    def tests(self):
        with self.directory() as kdir:
            kdir.menu(L('API Tree'), call(self.listing, 2))
            kdir.menu('VoD', call(self.listing, 1785454))
            kdir.menu('Retransmisje', call(self.listing, 48583081))
        # 48583081 - retransmisje
        # self.listing(41055208)  # sezon
        # self.listing(1649941)  # seriale (mtr81)

    def _menu(self, kdir, pos=''):
        pos = [int(v) for v in pos.split(',') if v]
        menu = self.MENU
        for p in pos:
            menu = menu.items[p]
        for i, ent in enumerate(menu.items):
            if isinstance(ent, MenuItems):
                def order(it):
                    title = it.get('title', '').lower()
                    for k, vv in (ent.order or {}).items():
                        if type(vv) is str:
                            vv = (vv,)
                        for v in vv:
                            if fnmatch(v, title):
                                return -k
                    return 0

                # order = {v: -k for k, vv in (ent.order or {}).items() for v in ((vv,) if type(vv) is str else vv)}
                with kdir.items_block() as blk:
                    for j, it in enumerate(self._get_items(ent.id)):
                        self._item(kdir, it, custom=(i, order(it), j))
                    blk.sort_items(key=lambda item: item.custom)
            elif ent.call:
                kdir.menu(ent.title, getattr(self, ent.call, ent.call), custom=(i,))
            elif ent.id:
                kdir.menu(ent.title, call(self.listing, ent.id))
            else:
                kdir.menu(ent.title, call(self.menu, i))

    def menu(self, pos: PathArg = ''):
        with self.directory() as kdir:
            self._menu(kdir, pos)

    def enter_listing(self, id: PathArg[int]):
        # type = 0  - ShowAndGetNumber
        n = xbmcgui.Dialog().numeric(0, 'ID', str(id))
        if n:
            n = int(n)
            if n > 0:
                self.refresh(call(self.listing, n))

    def listing(self, id: PathArg[int], type=None):
        """Use api.v3.tvp.pl JSON listing."""
        with self.directory() as kdir:
            kdir.item(f'=== {id}', call(self.enter_listing, id=id))  # XXX DEBUG
            # data = self.site.jget(None, params={'count': self.limit, 'parent_id': id})
            data = self._get(id)
            items = data.get('items') or ()
            if items:
                parents = items[0]['parents'][1:]
                if parents:
                    kdir.menu('^^^', call(self.listing, id=parents[0]))  # XXX DEBUG
            if len(items) == 1 and items[0].get('object_type') == 'directory_video' and items[0]['title'] == 'wideo':
                # Oszukany katalog sezonu, pokaż id razu odcinki.
                data = self.site.jget(None, params={'count': self.limit, 'parent_id': items[0]['asset_id']})
                items = data.get('items') or ()

            # Zwykłe katalogi (albo odcinki bezpośrenio z oszukanego).
            for item in items:
                self._item(kdir, item)

    @entry(title=L('TV'))
    def tv(self):
        """TV channel list."""
        with self.directory() as kdir:
            for ch in self.channel_iter():
                kdir.menu(ch.name, self.tv, image=ch.img)

    def _get(self, id):
        return self.site.jget(None, params={'count': self.limit, 'parent_id': id})

    def _get_items(self, id):
        for it in self._get(id).get('items') or ():
            yield it

    def _item(self, kdir, item, *, custom=None):
        itype = item.get('object_type')
        # format title
        title = item['title']
        if item.get('website_title'):
            title = f'{item["website_title"]}: {title}'
        elif item.get('title_root'):
            title = item['title_root']
        if title[:1].islower():
            title = title[0].upper() + title[1:]
        title = f'{title} [COLOR gray]({itype or "???"})[/COLOR]'  # XXX DEBUG
        # image
        image = None
        for img_attr in ('image', 'image_16x9', *(key for key in item if key.startswith('image_'))):
            for img_data in item.get(img_attr) or ():
                image = image_link(img_data)
                if image:
                    break
            else:  # double-for break trick
                continue
            break
        # description
        descr = item.get('lead_root') or item.get('description_root')
        descr = remove_tags(descr)
        if 'commentator' in item:
            descr += '\n\n[B]Komentarz[/B]\n' + item['commentator']
        # item
        if itype == 'video':
            kdir.play(title, call(self.video, id=item['asset_id']), image=image, descr=descr, custom=custom)
        else:
            kdir.menu(title, call(self.listing, id=item['asset_id']), image=image, descr=descr, custom=custom)

    def X_home(self):
        with self.directory() as kdir:
            page = self.vod.txtget('')
            for url, title in dom_select(page, 'ul.mainMenu .mainMenuItem:first-child .subMenu li a(href)::text'):
                kdir.menu(title.strip().capitalize(), call(self.category, id=linkid(url)))
                # kdir.menu(title.strip().capitalize(), call(self.listing, type='category', id=linkid(url)))

    def X_category(self, id: PathArg):
        with self.directory() as kdir:
            page = self.vod.txtget(f'category/x,{id}')
            for url, title in dom_select(page, 'section[data-id] h2 a(href)::text'):
                kdir.menu(title.strip(), call(self.subcategory, id=linkid(url)))

    def X_subcategory(self, id: PathArg):
        # sel = ('div.strefa-abo__item { a(href), h3::text, img.strefa-abo__img(src),'
        #        ' .strefa-abo__item-content(data-hover) }')
        with self.directory() as kdir:
            page = self.vod.txtget(f'sub-category/x,{id}')
            # for url, title, img, info in dom_select(page, sel):
            for info in dom_select(page, '.strefa-abo__item-content(data-hover)'):
                info = Info.parse(info)
                if info:
                    # TODO: add to context-menu play episode from info['episodeLink']
                    kdir.menu(info.title, call(self.listing, type=info.type, id=info.linkid),
                              image=info.image, descr=info.descr)

    def X_v_listing(self, type: PathArg, id: PathArg):
        with self.directory() as kdir:
            U = self.vod.base / f'{type}/x,{id}'
            log(f'VOD:  {U}')
            page = self.vod.txtget(f'{type}/x,{id}')
            for info in dom_select(page, '.js-hover(data-hover)'):
                info = Info.parse(info)
                if info:
                    title = f'{info.title} ({", ".join(info["types"])})'
                    kdir.menu(title, call(self.listing, type=info.type, id=info.linkid),
                              image=info.image, descr=info.descr)

    def X_website(self, id: PathArg):
        with self.directory() as kdir:
            page = self.vod.txtget(f'website/x,{id}')
            for info in dom_select(page, '.strefa-abo__item-content(data-hover)'):
                info = Info.parse(info)
                if info:
                    kdir.menu(info.title, call(self.video, id=info.linkid), image=info.image,
                              descr=info.descr)

    def X_series(self, id: PathArg, season: PathArg = None):
        with self.directory() as kdir:
            if season is None:
                page = self.vod.txtget(f'website/x,{id}/video', params={'season': season})
                for url, title in dom_select(page, '.episodes .dropdown-menu li a(href)::text'):
                    title = title.strip().capitalize()
                    kdir.menu(title, call(self.series, id=id, season=linkid(url)))
            else:
                page = self.vod.txtget(f'website/x,{id}')

            for info in dom_select(page, '.js-hover(data-hover)'):
                try:
                    info = Info.parse(info)
                except (json.JSONDecodeError, KeyError) as exc:
                    log.warning(f'Can not parse video info {exc} from: {info!r}')
                    continue
                kdir.menu(info.title, call(self.video, id=info.linkid), image=info.image,
                          descr=info.descr)

    # def video(self, id: PathArg):
    #     with self.directory() as kdir:
    #         kdir.item('Tu nic jeszcze nie ma !!!', call(self.series, 0))

    def video(self, id: PathArg[int]):
        """Play video – PlayTVPInfo by mtr81."""
        site = Site()
        url = f'https://www.tvp.pl/shared/cdn/tokenizer_v2.php?object_id={id}&sdt_version=1'
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
                                subt = subt_gen_ABO(d)
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

    def channel_iter(self):
        """JSON-live channel list."""
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
            "query": '''
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
}
''',
        }
        eee = {
            "operationName": None,
            "variables": {
                "stationCode": '',
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "0b9649840619e548b01c33ae4bba6027f86eac5c48279adc04e9ac2533781e6b",
                    },
                },
            }
        }
        data = self.site.jpost('https://hbb-prod.tvp.pl/apps/manager/api/hub/graphql', json=query)
        log(f'data\n{data!r}')
        for ch in data['data']['getStationsForMainpage']['items']:
            code = ch['code']
            name = ch['name']
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
