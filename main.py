
from libka import L, Plugin, Site
from libka import call, PathArg
from libka.logs import log
from pdom import select as dom_select
import json
from collections import namedtuple
from html import unescape


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


def linkid(url):
    """Returns ID from TVP link."""
    return url.rpartition(',')[2]


class Info(namedtuple('Info', 'data type url title image descr series linkid')):

    @classmethod
    def parse(cls, data):
        data = json.loads(unescape(data))
        eLink = data.get('episodeLink')
        sLink = data.get('seriesLink')
        url = sLink
        # 'episodeCount'
        # TODO:  dodać analizę w zlaezności od typu i różnic w obu linkach
        #        np. "video" i takie same linki wskazuję bezpośrednio film
        return Info(data, type=data['type'], url=url, title=data['title'], image=data['image'],
                    descr=data.get('description'), series=(eLink != sLink),
                    linkid=url.rpartition(',')[2])


class TvpVodSite(Site):
    """vod.tvp.pl site."""


class TvpPlugin(Plugin):
    """tvp.pl plugin."""

    def __init__(self):
        super().__init__()
        self.vod = Site(base='https://vod.tvp.pl')

    def home(self):
        with self.directory() as kdir:
            page = self.vod.txtget('')
            for url, title in dom_select(page, 'ul.mainMenu .mainMenuItem:first-child .subMenu li a(href)::text'):
                kdir.menu(title.strip().capitalize(), call(self.category, id=linkid(url)))

    def category(self, id: PathArg):
        with self.directory() as kdir:
            page = self.vod.txtget(f'category/x,{id}')
            for url, title in dom_select(page, 'section[data-id] h2 a(href)::text'):
                kdir.menu(title.strip(), call(self.subcategory, id=linkid(url)))

    def subcategory(self, id: PathArg):
        # sel = ('div.strefa-abo__item { a(href), h3::text, img.strefa-abo__img(src),'
        #        ' .strefa-abo__item-content(data-hover) }')
        with self.directory() as kdir:
            page = self.vod.txtget(f'sub-category/x,{id}')
            # for url, title, img, info in dom_select(page, sel):
            for info in dom_select(page, '.strefa-abo__item-content(data-hover)'):
                try:
                    info = Info.parse(info)
                except (json.JSONDecodeError, KeyError) as exc:
                    log.warning(f'Can not parse video info {exc} from: {info!r}')
                    continue
                # TODO: add to context-menu play episode from info['episodeLink']
                if info.series:
                    kdir.menu(info.title, call(self.series, id=info.linkid), image=info.image,
                              descr=info.descr)
                else:
                    kdir.menu(info.title, call(self.video, id=info.linkid), image=info.image,
                              descr=info.descr)

    def series(self, id: PathArg):
        with self.directory() as kdir:
            page = self.vod.txtget(f'website/x,{id}')
            for info in dom_select(page, '.strefa-abo__item-content(data-hover)'):
                try:
                    info = Info.parse(info)
                except (json.JSONDecodeError, KeyError) as exc:
                    log.warning(f'Can not parse video info {exc} from: {info!r}')
                    continue
                kdir.menu(info.title, call(self.video, id=info.linkid), image=info.image,
                          descr=info.descr)

    def video(self, id: PathArg):
        with self.directory() as kdir:
            kdir.item('Tu nic jeszcze nie ma !!!', call(self.series, 0))


# DEBUG ONLY
import sys  # noqa
log(f'TVP: ENTER {sys.argv}')

# Create and run plugin.
TvpPlugin().run()
