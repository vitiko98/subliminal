# -*- coding: utf-8 -*-
import io
import logging
import os
import zipfile

import rarfile
from babelfish import Language
from guessit import guessit
from requests import Session

from ..exceptions import ProviderError
from ..matches import guess_matches
from ..subtitle import Subtitle, fix_line_ending
from ..video import Episode
from . import Provider

logger = logging.getLogger(__name__)

SERVER_URL = "http://sapidb.caretas.club"
PAGE_URL = "https://sucha.caretas.club"


class SuchaSubtitle(Subtitle):
    provider_name = "sucha"
    hash_verifiable = False

    def __init__(
        self,
        language,
        release_info,
        filename,
        download_id,
        download_type,
        matches,
    ):
        super(SuchaSubtitle, self).__init__(
            language, hearing_impaired=False, page_link=PAGE_URL
        )
        self.download_id = download_id
        self.download_type = download_type
        self.language = language
        self.guessed_release_info = release_info
        self.filename = filename
        self.release_info = (
            release_info if len(release_info) > len(filename) else filename
        )
        self.found_matches = matches

    @property
    def id(self):
        return self.download_id

    @property
    def info(self):
        return self.release_info

    def get_matches(self, video):
        self.found_matches |= guess_matches(
            video,
            guessit(
                self.filename,
                {"type": "episode" if isinstance(video, Episode) else "movie"},
            ),
        )
        self.found_matches |= guess_matches(
            video,
            guessit(
                self.guessed_release_info,
                {"type": "episode" if isinstance(video, Episode) else "movie"},
            ),
        )
        return self.found_matches


class SuchaProvider(Provider):
    """Sucha Provider"""

    languages = {Language.fromalpha2(l) for l in ["es"]}
    language_list = list(languages)

    def initialize(self):
        self.session = Session()
        self.session.headers.update(
            {"User-Agent": os.environ.get("SZ_USER_AGENT", "Sub-Zero/2")}
        )

    def terminate(self):
        self.session.close()

    def query(self, languages, video):
        movie_year = video.year or "0"
        is_episode = isinstance(video, Episode)
        type_str = "episode" if is_episode else "movie"
        language = self.language_list[0]

        if is_episode:
            q = {"query": f"{video.series} S{video.season:02}E{video.episode:02}"}
        else:
            q = {"query": video.title, "year": movie_year}

        logger.debug(f"Searching subtitles: {q}")
        result = self.session.get(f"{SERVER_URL}/{type_str}", params=q, timeout=10)
        result.raise_for_status()

        results = result.json()
        if isinstance(results, dict):
            logger.debug("No subtitles found")
            return []

        subtitles = []
        for item in results:
            matches = set()
            title = item.get("title", "").lower()
            alt_title = item.get("alt_title", title).lower()

            if any(video.title.lower() in item for item in (title, alt_title)):
                matches.add("title")

            if str(item["year"]) == video.year:
                matches.add("year")

            if is_episode and any(
                q["query"].lower() in item for item in (title, alt_title)
            ):
                matches.update(("title", "series", "season", "episode", "year"))

            subtitles.append(
                SuchaSubtitle(
                    language,
                    item["release"],
                    item["filename"],
                    str(item["id"]),
                    type_str,
                    matches,
                )
            )
        return subtitles

    def list_subtitles(self, video, languages):
        return self.query(languages, video)

    def _check_response(self, response):
        if response.status_code != 200:
            raise ProviderError("Bad status code: " + str(response.status_code))

    def _get_archive(self, content):
        archive_stream = io.BytesIO(content)
        if rarfile.is_rarfile(archive_stream):
            logger.debug("Identified rar archive")
            archive = rarfile.RarFile(archive_stream)
        elif zipfile.is_zipfile(archive_stream):
            logger.debug("Identified zip archive")
            archive = zipfile.ZipFile(archive_stream)
        else:
            raise ValueError("Unsupported compressed format")
        return archive

    def get_file(self, archive):
        for name in archive.namelist():
            if os.path.split(name)[-1].startswith("."):
                continue
            if not name.lower().endswith(".srt"):
                continue
            if (
                "[eng]" in name.lower()
                or ".en." in name.lower()
                or ".eng." in name.lower()
            ):
                continue
            logger.debug("Returning from archive: {}".format(name))
            return archive.read(name)
        raise ValueError("Can not find the subtitle in the compressed file")

    def download_subtitle(self, subtitle):
        logger.info("Downloading subtitle %r", subtitle)
        response = self.session.get(
            f"{SERVER_URL}/download",
            params={"id": subtitle.download_id, "type": subtitle.download_type},
            timeout=10,
        )
        response.raise_for_status()
        self._check_response(response)
        archive = self._get_archive(response.content)
        subtitle_file = self.get_file(archive)
        subtitle.content = fix_line_ending(subtitle_file)
