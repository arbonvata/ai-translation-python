"""DeepL and Go proxy requests, batching, and response validation."""

from dataclasses import dataclass

import requests

from .android_xml import translated_resource, translation_value
from .models import StringResource, TranslationError, resource_label

DEEPL_FREE_ENDPOINT = "https://api-free.deepl.com/v2/translate"
DEEPL_PRO_ENDPOINT = "https://api.deepl.com/v2/translate"
MAX_TEXTS_PER_REQUEST = 50


@dataclass(frozen=True)
class TranslationClient:
    """Route translations using an explicit, reusable configuration."""

    api_key: str
    base_url: str | None = None
    override: bool = False

    def __post_init__(self):
        if self.override and not self.base_url:
            raise TranslationError("--override requires DEEPL_BASE_URL (the Go translation proxy).")
        if not self.api_key:
            raise TranslationError(
                "DEEPL_API_KEY environment variable is not set. Set a real DeepL key, "
                "or set DEEPL_BASE_URL and use the proxy auth key (any non-empty value "
                "when the server does not require authentication)."
            )

    def translate(self, resources: list[StringResource], language: str) -> list[StringResource]:
        if self.base_url:
            return proxy_translate_resources(
                resources, language, self.base_url, self.api_key, override=self.override
            )
        return translate_resources(resources, language, self.api_key)


def batches(items):
    """Yield DeepL-sized slices without changing their order."""
    for start in range(0, len(items), MAX_TEXTS_PER_REQUEST):
        yield items[start:start + MAX_TEXTS_PER_REQUEST]


def translation_options(target_language, source_language="EN", use_xml=False):
    options = {
        "source_lang": source_language,
        "target_lang": target_language.upper(),
        "preserve_formatting": True,
    }
    if use_xml:
        options.update(tag_handling="xml", ignore_tags=["xliff:g"])
    return options


def request_translations(endpoint, payload, api_key, *, timeout, service):
    """Centralize transport errors and validate the common response shape."""
    language = payload["target_lang"]
    try:
        response = requests.post(
            endpoint, json=payload,
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"}, timeout=timeout,
        )
    except requests.RequestException as error:
        raise TranslationError(f"{service} request failed for '{language}': {error}") from error
    try:
        try:
            response.raise_for_status()
        except requests.RequestException as error:
            raise TranslationError(
                f"{service} error for '{language}': {error} {response.text}"
            ) from error
        try:
            result = response.json()
        except ValueError as error:
            raise TranslationError(f"{service} returned invalid JSON for '{language}'") from error
        items = result.get("translations") if isinstance(result, dict) else None
        if not isinstance(items, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("text"), str)
            for item in items
        ):
            raise TranslationError(f"{service} returned invalid translations for '{language}'")
        return items
    finally:
        response.close()


def deepl_translate_texts(texts, target_language, api_key, source_language="EN",
                          context=None, ids=None, use_xml=False):
    """Translate batches directly; DeepL matches responses by position, not ID."""
    endpoint = DEEPL_FREE_ENDPOINT if api_key.endswith(":fx") else DEEPL_PRO_ENDPOINT
    translations = []
    for start in range(0, len(texts), MAX_TEXTS_PER_REQUEST):
        chunk = texts[start:start + MAX_TEXTS_PER_REQUEST]
        payload = translation_options(target_language, source_language, use_xml)
        payload["text"] = chunk
        if context:
            payload["context"] = context
        items = request_translations(endpoint, payload, api_key, timeout=30, service="DeepL API")
        if len(items) != len(chunk):
            labels = ids[start:start + MAX_TEXTS_PER_REQUEST] if ids else []
            span = "..".join((labels[0], labels[-1])) if labels else "unidentified texts"
            raise TranslationError(
                f"DeepL returned {len(items)} translations for {len(chunk)} texts "
                f"({span}) to '{target_language}' — aborting"
            )
        translations.extend(item["text"] for item in items)
    return translations


def direct_translation_groups(resources):
    """Keep arrays together so ambiguous items share their array's context."""
    plain = [i for i, resource in enumerate(resources) if resource.type != "string-array"]
    yield plain, None
    arrays = {}
    for i, resource in enumerate(resources):
        if resource.type == "string-array":
            arrays.setdefault(resource.name, []).append(i)
    for indices in arrays.values():
        context = resources[indices[0]].context
        yield indices, None if context == "No context found" else context


def translate_resources(resources: list[StringResource], target_language: str,
                        api_key: str) -> list[StringResource]:
    """Translate values while preserving resource metadata and original order."""
    use_xml = any(resource.markup for resource in resources)
    translated = list(resources)
    for indices, context in direct_translation_groups(resources):
        values = deepl_translate_texts(
            [translation_value(resources[i], use_xml) for i in indices],
            target_language, api_key, context=context,
            ids=[resource_label(resources[i]) for i in indices], use_xml=use_xml,
        )
        for i, value in zip(indices, values):
            translated[i] = translated_resource(resources[i], value, use_xml)
    return translated


def proxy_payload(resources, target_language, source_language, use_xml, override):
    payload = translation_options(target_language, source_language, use_xml)
    payload["texts"] = [
        {
            "id": resource_label(resource),
            "text": translation_value(resource, use_xml),
            **({"context": resource.context}
               if resource.context and resource.context != "No context found" else {}),
        }
        for resource in resources
    ]
    # This cache-control flag belongs only to the Go proxy, never to DeepL.
    if override:
        payload["override"] = True
    return payload


def match_proxy_translations(resources, items, use_xml):
    by_id = {}
    for item in items:
        label = item.get("id")
        if not isinstance(label, str):
            raise TranslationError("Translation proxy returned a translation without a string ID")
        if label in by_id:
            raise TranslationError(f"Translation proxy returned '{label}' twice — aborting")
        by_id[label] = item["text"]
    translated = []
    for resource in resources:
        label = resource_label(resource)
        if label not in by_id:
            raise TranslationError(f"Translation proxy returned nothing for '{label}' — aborting")
        translated.append(translated_resource(resource, by_id[label], use_xml))
    return translated


def proxy_translate_resources(resources: list[StringResource], target_language: str,
                              base_url: str, api_key: str, source_language: str = "EN",
                              override: bool = False) -> list[StringResource]:
    """Match proxy responses by ID; override refreshes every requested cache entry."""
    endpoint = f"{base_url.rstrip('/')}/v2/translate"
    translated = []
    for chunk in batches(resources):
        use_xml = any(resource.markup for resource in chunk)
        payload = proxy_payload(chunk, target_language, source_language, use_xml, override)
        items = request_translations(endpoint, payload, api_key, timeout=300,
                                     service="Translation proxy")
        translated.extend(match_proxy_translations(chunk, items, use_xml))
    return translated
