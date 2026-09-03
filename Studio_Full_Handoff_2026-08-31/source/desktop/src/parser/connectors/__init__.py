"""Реестр коннекторов источников. Новый источник = новый модуль + строка здесь."""
from . import (archive_movies, commons, commons_cat, ddb, europeana,
               internet_archive, kramerius, loc, nara, pexels, pixabay,
               youtube_cc)

REGISTRY = {
    "archive_movies": archive_movies.ArchiveMoviesConnector,
    "youtube_cc": youtube_cc.YoutubeCCConnector,
    "commons": commons.CommonsConnector,
    "commons_cat": commons_cat.CommonsCatConnector,
    "europeana": europeana.EuropeanaConnector,
    "loc": loc.LocConnector,
    "pexels": pexels.PexelsConnector,
    "pixabay": pixabay.PixabayConnector,
    "nara": nara.NaraConnector,
    "internet_archive": internet_archive.InternetArchiveConnector,
    "ddb": ddb.DdbConnector,
    "kramerius": kramerius.KrameriusConnector,
}


def get_connector(name: str, keys: dict, throttle: float = 0.7):
    cls = REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"неизвестный источник: {name}")
    return cls(keys=keys, throttle=throttle)
