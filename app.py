import os
import re
import json
import threading
import importlib
import urllib.parse
import urllib.request
import time

import gradio as gr
try:
    import spaces
    HAS_SPACES = True
except ImportError:
    HAS_SPACES = False
from fastapi import Request, APIRouter
from fastapi.responses import JSONResponse, StreamingResponse, Response

# Configure environment for clean, fast ani-tupi execution
os.environ['ANI_TUPI_FAST_MODE'] = '1'
os.environ['ANI_TUPI__UPDATE_CHECK__ENABLED'] = 'false'
os.environ['ANI_TUPI__SEARCH__ENABLE_TITLE_RESOLUTION'] = 'false'
os.environ['ANI_TUPI__ANILIST__ANILIST_AUTO_DISCOVER'] = 'false'
os.environ['ANI_TUPI__CACHE__ENABLED'] = 'false'

ALL_PLUGINS = [
    'animefire',
    'animesdigital',
    'animesonlinecc',
    'animesonlineio',
    'anitube',
    'anroll',
    'dattebayo',
    'goyabu',
    'otakulogia',
    'sushianimes',
]

PRIORITY_ORDER = [
    'animesdigital',
    'anroll',
    'sushianimes',
    'animefire',
    'animesonlinecc',
    'animesonlineio',
    'dattebayo',
    'anitube',
    'otakulogia',
    'goyabu',
]

_rep_lock = threading.Lock()
_rep_instance = None
_patches_installed = False


def _ensure_runtime_patches():
    """Lazily apply concurrency & TLS patches only when first API request arrives."""
    global _patches_installed
    if _patches_installed:
        return
    _patches_installed = True

    try:
        import services.repository.search_repository as sr
        sr.cpu_count = lambda: 10
    except Exception as e:
        print("Aviso patch sr.cpu_count:", e)

    try:
        import httpx
        import scrapers.plugins.utils as plugin_utils

        _orig_http_request = plugin_utils.http_request_with_retry

        try:
            import cloudscraper
            _cf_scraper = cloudscraper.create_scraper(
                browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
            )
            has_cloudscraper = True
        except ImportError:
            has_cloudscraper = False

        def _smart_http_request_with_retry(
            method: str,
            url: str,
            *,
            headers: dict | None = None,
            json: dict | None = None,
            data: bytes | str | None = None,
            params: dict | None = None,
            timeout: float = 12,
            follow_redirects: bool = True,
            max_retries: int = 1,
            backoff_base: float = 0.3,
            client: httpx.Client | None = None,
        ) -> httpx.Response:
            effective_timeout = min(float(timeout or 12), 12.0)
            req_headers = dict(headers or plugin_utils.DEFAULT_HEADERS)
            if "User-Agent" not in req_headers or "Linux" in req_headers.get("User-Agent", ""):
                req_headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            req_headers.setdefault("Accept-Language", "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7")

            try:
                return _orig_http_request(
                    method,
                    url,
                    headers=req_headers,
                    json=json,
                    data=data,
                    params=params,
                    timeout=effective_timeout,
                    follow_redirects=follow_redirects,
                    max_retries=min(max_retries, 1),
                    backoff_base=backoff_base,
                    client=client,
                )
            except Exception as orig_err:
                if has_cloudscraper and client is None:
                    try:
                        cs_resp = _cf_scraper.request(
                            method=method,
                            url=url,
                            headers=req_headers,
                            json=json,
                            data=data,
                            params=params,
                            timeout=effective_timeout,
                            allow_redirects=follow_redirects,
                        )
                        if cs_resp.status_code < 400:
                            return httpx.Response(
                                status_code=cs_resp.status_code,
                                headers=dict(cs_resp.headers),
                                content=cs_resp.content,
                                request=httpx.Request(method, str(cs_resp.url or url)),
                            )
                    except Exception:
                        pass
                raise orig_err

        def _smart_http_get_with_retry(
            url: str,
            *,
            headers: dict | None = None,
            timeout: float = 12,
            follow_redirects: bool = True,
            max_retries: int = 1,
            backoff_base: float = 0.3,
            client: httpx.Client | None = None,
        ) -> httpx.Response:
            return _smart_http_request_with_retry(
                "GET",
                url,
                headers=headers,
                timeout=timeout,
                follow_redirects=follow_redirects,
                max_retries=max_retries,
                backoff_base=backoff_base,
                client=client,
            )

        plugin_utils.http_request_with_retry = _smart_http_request_with_retry
        plugin_utils.http_get_with_retry = _smart_http_get_with_retry
    except Exception as e:
        print("Aviso patch HTTP:", e)


def get_rep():
    global _rep_instance
    _ensure_runtime_patches()
    if _rep_instance is None:
        from services.repository import rep
        try:
            rep._search._try_cache = lambda query, verbose=True: False
            rep._search._save_cache = lambda query, verbose=True: None
        except Exception:
            pass
        for plugin_name in ALL_PLUGINS:
            try:
                mod = importlib.import_module(f"scrapers.plugins.{plugin_name}")
                mod.load(rep.register)
            except Exception:
                pass
        _rep_instance = rep
    return _rep_instance


def clean_query(q):
    if not q:
        return ""
    cleaned = re.sub(r'[.!?;,~…]+', ' ', q)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned or q


STOP_WORDS = {
    'the', 'and', 'for', 'with', 'kun', 'chan', 'san', 'ni', 'no',
    'wa', 'ga', 'wo', 'to', 'de', 'da', 'do', 'das', 'dos', 'em', 'na', 'no',
    'nas', 'nos', 'season', 'part', 'dublado', 'legendado', 'todos', 'episodios'
}


def get_query_keywords(q):
    words = [w.lower() for w in re.findall(r'[a-zA-Z0-9]{2,}', q)]
    keywords = [w for w in words if w not in STOP_WORDS]
    return keywords if keywords else words


def normalize_title_for_match(text):
    text = re.sub(r'\s*\([^)]*\)|\s*\[[^\]]*\]', '', text)
    text = re.sub(r'-\s*todos os epis[oó]dios.*', '', text, flags=re.I)
    text = re.sub(r'\b(dublado|legendado|fhd|hd|bd|bluray)\b', '', text, flags=re.I)
    text = re.sub(r'[^a-zA-Z0-9\s]', ' ', text)
    return ' '.join(text.lower().split())


def is_relevant_title(candidate_title, keywords):
    if not keywords:
        return True

    cand_norm = normalize_title_for_match(candidate_title)
    cand_words = set(cand_norm.split())

    if len(keywords) >= 2:
        return all(kw in cand_words or any(kw in cw for cw in cand_words) for kw in keywords)

    kw = keywords[0]
    return kw in cand_words or any(kw in cw for cw in cand_words)


def calculate_release_score(query, title, sources_count=1):
    q_norm = normalize_title_for_match(query)
    t_norm = normalize_title_for_match(title)

    score = 0
    if t_norm == q_norm:
        score += 10000
    elif t_norm.startswith(q_norm):
        score += 5000 - min(4000, len(t_norm) - len(q_norm))
    else:
        score += 1000

    score += sources_count * 100
    return score


def sort_sources_by_priority(sources_list):
    return sorted(
        sources_list,
        key=lambda s: PRIORITY_ORDER.index(s) if s in PRIORITY_ORDER else 999
    )


def search_anime_smart(rep, query):
    cleaned = clean_query(query)
    keywords = get_query_keywords(cleaned)

    try:
        if getattr(rep._search, "last_query", None) == cleaned and len(getattr(rep._search, "anime_to_urls", {})) < 3:
            rep._search.last_query = None
    except Exception:
        pass

    rep.search_anime(cleaned)
    entries = rep.get_anime_titles_with_sources()
    relevant = [e for e in entries if is_relevant_title(e.split(' [')[0], keywords)]

    words = cleaned.split()
    if not relevant and len(words) > 2:
        short_query = " ".join(words[:2])
        rep.search_anime(short_query)
        entries = rep.get_anime_titles_with_sources()
        relevant = [e for e in entries if is_relevant_title(e.split(' [')[0], keywords)]

    return relevant


CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Range',
    'Access-Control-Allow-Private-Network': 'true',
}


def cors_json(payload, status_code=200):
    return JSONResponse(content=payload, status_code=status_code, headers=CORS_HEADERS)


def _dummy_gpu():
    return "Kizuna Cloud Bridge Online (24/7)"

if HAS_SPACES:
    check_bridge_gpu = spaces.GPU(_dummy_gpu)
else:
    check_bridge_gpu = _dummy_gpu


with gr.Blocks(title="Kizuna Cloud Bridge") as demo:
    gr.Markdown("# 🚀 Kizuna Cloud Bridge (24/7 Online)\nServidor de ponte ativo para o Kizuna Player.")
    btn = gr.Button("Verificar Status")
    out = gr.Textbox(label="Status")
    btn.click(fn=check_bridge_gpu, outputs=out)


def register_kizuna_routes(fastapi_app):
    router = APIRouter()

    @router.options("/api/goanime/{rest_of_path:path}")
    async def preflight_handler(rest_of_path: str):
        return Response(status_code=204, headers=CORS_HEADERS)

    @router.get("/api/goanime/status")
    def api_status():
        return cors_json({
            'online': True,
            'engine': 'ani-tupi-cloud',
            'hasAniTupi': True,
            'hasGoAnime': True,
            'version': 'Kizuna Cloud Engine v2.23.0 (Hugging Face 24/7)'
        })

    @router.get("/api/mangadex/{rest_of_path:path}")
    def api_mangadex_proxy(rest_of_path: str, request: Request):
        query_string = str(request.url.query)
        target_url = f"https://api.mangadex.org/{rest_of_path}"
        if query_string:
            target_url += f"?{query_string}"
        try:
            req = urllib.request.Request(
                target_url,
                headers={'User-Agent': 'KizunaPlayer/1.0.0 (https://kizuna-player.web.app)', 'Accept': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=12) as response:
                content = response.read()
                return Response(content=content, media_type="application/json", headers=CORS_HEADERS)
        except Exception as e:
            return cors_json({'error': str(e)}, 500)

    @router.get("/api/goanime/search")
    def api_search(title: str = ""):
        title = (title or "").strip()
        if not title:
            return cors_json({'success': False, 'error': 'Parâmetro title obrigatório'}, 400)
        try:
            from utils.title_normalization import dedup_signature
            with _rep_lock:
                rep = get_rep()
                titles_with_sources = search_anime_smart(rep, title)

            results = []
            for entry in titles_with_sources:
                if ' [' in entry and entry.endswith(']'):
                    r_title = entry.split(' [')[0]
                    sources_str = entry.split(' [')[1].rstrip(']')
                    sources = sort_sources_by_priority([s.strip() for s in sources_str.split(',') if s.strip()])
                else:
                    r_title = entry
                    sources = []

                _, season, langs = dedup_signature(r_title)
                is_dub = 'dub' in langs or 'dublado' in r_title.lower()

                results.append({
                    'raw': entry,
                    'title': r_title,
                    'sources': sources,
                    'isDubbed': is_dub,
                    'season': season or 1
                })

            results.sort(
                key=lambda r: calculate_release_score(title, r['title'], len(r.get('sources', []))),
                reverse=True
            )

            return cors_json({'success': True, 'query': title, 'results': results})
        except Exception as e:
            return cors_json({'success': False, 'error': str(e)}, 500)

    @router.get("/api/goanime/episodes")
    def api_episodes(animeTitle: str = "", releaseTitle: str = "", source: str = ""):
        anime_title = (animeTitle or "").strip()
        release_title = (releaseTitle or anime_title).strip()
        source_filter = (source or "").strip() or None

        if not anime_title:
            return cors_json({'success': False, 'error': 'Parâmetro animeTitle obrigatório'}, 400)
        try:
            with _rep_lock:
                rep = get_rep()
                search_anime_smart(rep, anime_title)
                rep.search_episodes(release_title, source_filter=source_filter)
                eps = rep.get_episode_list(release_title)
                eps_sorted = sorted(list(set(eps))) if eps else []

            return cors_json({
                'success': True,
                'animeTitle': anime_title,
                'releaseTitle': release_title,
                'source': source_filter,
                'episodes': eps_sorted,
                'count': len(eps_sorted)
            })
        except Exception as e:
            return cors_json({'success': False, 'error': str(e)}, 500)

    @router.post("/api/goanime/play")
    async def api_play(request: Request):
        try:
            payload = await request.json()
            main_title = (payload.get('animeTitle') or payload.get('title') or '').strip()
            target_release = (payload.get('releaseTitle') or main_title).strip()
            ep_num = int(payload.get('episode') or 1)
            source_param = payload.get('source')
            if source_param in ('', 'none', 'None', 'null'):
                source_param = None

            import concurrent.futures

            def _resolve_stream():
                with _rep_lock:
                    rep = get_rep()
                    search_anime_smart(rep, main_title)
                    rep.search_episodes(target_release, source_filter=source_param)
                    return rep.search_player(target_release, ep_num)

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                stream_url = ex.submit(_resolve_stream).result()

            if not stream_url:
                return cors_json({
                    'success': False,
                    'error': 'Não foi possível extrair o link de vídeo dos servidores.'
                }, 500)

            return cors_json({
                'success': True,
                'streamUrl': stream_url,
                'title': f"{target_release} - Ep {ep_num}",
                'player': 'Kizuna Player Nativo',
                'isNative': True,
                'pid': None
            })
        except Exception as e:
            return cors_json({'success': False, 'error': str(e)}, 500)

    @router.get("/api/goanime/stream-proxy")
    def api_stream_proxy(request: Request, url: str = ""):
        target_url = (url or "").strip()
        if not target_url:
            return cors_json({'error': 'URL de stream ausente'}, 400)
        try:
            parsed_target = urllib.parse.urlparse(target_url)
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                'Accept': '*/*',
                'Referer': f"{parsed_target.scheme}://{parsed_target.netloc}/"
            }
            range_hdr = request.headers.get('range') or request.headers.get('Range')
            if range_hdr:
                headers['Range'] = range_hdr

            req = urllib.request.Request(target_url, headers=headers, method='GET')
            upstream = urllib.request.urlopen(req, timeout=25)

            out_headers = dict(CORS_HEADERS)
            out_headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range, Accept-Ranges'
            for hdr in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
                val = upstream.headers.get(hdr)
                if val:
                    out_headers[hdr] = val

            def iter_stream():
                try:
                    while True:
                        chunk = upstream.read(65536)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    upstream.close()

            return StreamingResponse(
                iter_stream(),
                status_code=upstream.status,
                headers=out_headers
            )
        except Exception as e:
            return cors_json({'error': str(e)}, 502)

    if getattr(fastapi_app, "_kizuna_registered", False):
        return
    fastapi_app._kizuna_registered = True

    old_routes = list(fastapi_app.router.routes)
    fastapi_app.include_router(router)
    fastapi_app.include_router(router, prefix="/gradio_api")
    new_routes = [r for r in fastapi_app.router.routes if r not in old_routes]
    fastapi_app.router.routes = new_routes + old_routes
    fastapi_app.openapi_schema = None
    print("✅ Rotas do Kizuna Cloud Bridge registradas com prioridade máxima!")


# Hook Gradio App creation BEFORE demo.launch() (no background threads!)
try:
    _orig_create_app = gr.routes.App.create_app

    def _hooked_create_app(*args, **kwargs):
        fastapi_app = _orig_create_app(*args, **kwargs)
        try:
            register_kizuna_routes(fastapi_app)
        except Exception as e:
            print("Erro ao registrar rotas no create_app:", e)
        return fastapi_app

    gr.routes.App.create_app = staticmethod(_hooked_create_app)
except Exception as e:
    print("Aviso hook create_app:", e)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7860))
    demo.launch(
        server_name="0.0.0.0",
        server_port=port,
        ssr_mode=False
    )
