from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Response

from creator_enrichment.constants import (
    CONTACT_API_PATH,
    PAUSE_REASON_CAPTCHA,
    PAUSE_REASON_REGION_MISMATCH,
    PROFILE_API_PATH,
    STATE_STATUS_PAUSED_CAPTCHA,
    STATE_STATUS_PAUSED_REGION_MISMATCH,
    STATE_STATUS_SUCCESS,
)
from creator_enrichment.parsers import (
    contact_info_available,
    contact_patch,
    nested_value,
    normalized_creator_id,
    normalized_region,
    profile_request_metadata,
    query_param,
    should_capture_profile,
)
from creator_enrichment.state import update_item_state


@dataclass(slots=True)
class _CapturedProfile:
    creator_id: str
    payload: dict[str, object]
    shop_region: str
    profile_types: tuple[int, ...]
    page_url: str


@dataclass(slots=True)
class _CapturedContact:
    creator_id: str
    payload: dict[str, object]
    page_url: str


class CapturePipelineMixin:
    def _handle_response(self, response: Response) -> None:
        managed_page = self._response_page(response)
        if managed_page is None or managed_page is not self._page:
            return
        task_index = self._assigned_task_index(managed_page)
        if task_index is None:
            return
        try:
            path = urlsplit(response.url).path
        except ValueError:
            return
        try:
            payload = response.json()
        except (PlaywrightError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        if path.endswith(PROFILE_API_PATH):
            creator_id, profile_types = profile_request_metadata(response)
            shop_region = query_param(response.url, "shop_region")
            profile = payload.get("creator_profile")
            if not isinstance(profile, dict):
                profile = {}
            response_creator_id = normalized_creator_id(
                nested_value(profile.get("creator_oecuid"))
                or query_param(self._page_url(managed_page), "cid")
            )
            should_capture = should_capture_profile(
                profile=profile,
                request_creator_id=creator_id,
                response_creator_id=response_creator_id,
                profile_types=profile_types,
            )
            if should_capture:
                self._log_event(
                    "profile_seen",
                    task_index=task_index,
                    source="response",
                )
                self._store_profile_capture(
                    task_index=task_index,
                    creator_id=response_creator_id or creator_id,
                    payload=payload,
                    shop_region=shop_region,
                    profile_types=profile_types,
                    page_url=self._page_url(managed_page),
                )
            return
        if path.endswith(CONTACT_API_PATH):
            creator_id = query_param(response.url, "creator_oecuid")
            if creator_id:
                self._log_event(
                    "contact_seen",
                    task_index=task_index,
                    source="response",
                )
                self._store_contact_capture(
                    task_index=task_index,
                    creator_id=creator_id,
                    payload=payload,
                    page_url=self._page_url(managed_page),
                )

    def _pull_page_captures(self) -> None:
        if self._page is None:
            return
        try:
            captures = self._page.evaluate(
                """
                () => {
                    const root = window.__linkGlancerCapture;
                    const invalidRoot = !root
                        || !Array.isArray(root.profileResponses)
                        || !Array.isArray(root.contactResponses);
                    if (invalidRoot) {
                        return { profileResponses: [], contactResponses: [] };
                    }
                    const profileResponses = root.profileResponses.splice(
                        0,
                        root.profileResponses.length,
                    );
                    const contactResponses = root.contactResponses.splice(
                        0,
                        root.contactResponses.length,
                    );
                    return { profileResponses, contactResponses };
                }
                """
            )
        except PlaywrightError:
            return
        if not isinstance(captures, dict):
            return
        task_index = self._work_page_task_index
        profile_responses = captures.get("profileResponses")
        if isinstance(profile_responses, list):
            for payload in profile_responses:
                self._handle_page_profile_capture(task_index, payload)
        contact_responses = captures.get("contactResponses")
        if isinstance(contact_responses, list):
            for payload in contact_responses:
                self._handle_page_contact_capture(task_index, payload)
        self._process_captured_events(trigger="poll")

    def _handle_page_profile_capture(self, task_index: int | None, entry: object) -> None:
        if task_index is None or not isinstance(entry, dict):
            return
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        profile = payload.get("creator_profile")
        if not isinstance(profile, dict):
            profile = {}
        creator_id = normalized_creator_id(nested_value(profile.get("creator_oecuid")))
        if not creator_id:
            creator_id = query_param(str(entry.get("pageUrl") or ""), "cid")
        if not creator_id:
            return
        shop_region = query_param(str(entry.get("url") or ""), "shop_region") or query_param(
            str(entry.get("pageUrl") or ""),
            "shop_region",
        )
        raw_profile_types = entry.get("profileTypes")
        profile_types: tuple[int, ...] = ()
        if isinstance(raw_profile_types, list):
            parsed_types: list[int] = []
            for item in raw_profile_types:
                try:
                    parsed_types.append(int(item))
                except (TypeError, ValueError):
                    continue
            profile_types = tuple(parsed_types)
        self._last_profile_response_url = str(entry.get("url") or "")
        self._last_profile_payload = payload
        self._last_profile_types = profile_types
        self._last_profile_seen_at = datetime.now(UTC)
        if should_capture_profile(
            profile=profile,
            request_creator_id=creator_id,
            response_creator_id=creator_id,
            profile_types=profile_types,
        ):
            self._log_event(
                "profile_seen",
                task_index=task_index,
                source="page_capture",
            )
            self._store_profile_capture(
                task_index=task_index,
                creator_id=creator_id,
                payload=payload,
                shop_region=shop_region,
                profile_types=profile_types,
                page_url=str(entry.get("pageUrl") or ""),
            )

    def _handle_page_contact_capture(self, task_index: int | None, entry: object) -> None:
        if task_index is None or not isinstance(entry, dict):
            return
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        url = str(entry.get("url") or "")
        creator_id = query_param(url, "creator_oecuid") or normalized_creator_id(
            entry.get("creatorId")
        )
        if not creator_id:
            return
        self._last_contact_response_url = url
        self._last_contact_payload = payload
        self._contact_positive_signal = True
        self._log_event(
            "contact_seen",
            task_index=task_index,
            source="page_capture",
        )
        self._store_contact_capture(
            task_index=task_index,
            creator_id=creator_id,
            payload=payload,
            page_url=str(entry.get("pageUrl") or ""),
        )

    def _process_captured_events(self, *, trigger: str) -> None:
        if self._paused or self._completed or self._current_task_index is None:
            return
        if not self._attempt_in_progress or self._is_task_finalizing(self._current_task_index):
            return
        self._discard_stale_captures()
        self._log_event(
            "process_captured_events",
            trigger=trigger,
            has_profile=self._captured_profile is not None,
            has_contact=self._captured_contact is not None,
            step=self._current_step,
        )
        if self._current_step == "waiting_profile" and self._captured_profile is not None:
            self._apply_profile()
            return
        if self._current_step == "waiting_contact_badge":
            if self._captured_contact is not None:
                self._current_step = "waiting_contact"
                self._contact_positive_signal = True
            elif self._last_contact_badge_clicked:
                self._current_step = "waiting_contact"
                self._waiting_started_at = datetime.now(UTC)
                self._last_message = self._with_subject("有联系方式，正在采集。")
        if self._current_step == "waiting_contact":
            if self._captured_profile is not None and not self._profile_patch_applied:
                self._apply_profile()
                if self._paused or self._completed:
                    return
            if self._captured_contact is not None and self._profile_patch_applied:
                self._apply_contact()

    def _store_profile_capture(
        self,
        *,
        task_index: int,
        creator_id: str,
        payload: dict[str, object],
        shop_region: str,
        profile_types: tuple[int, ...],
        page_url: str,
    ) -> None:
        if self._is_task_finalizing(task_index):
            return
        captured = _CapturedProfile(
            creator_id=creator_id,
            payload=payload,
            shop_region=shop_region,
            profile_types=profile_types,
            page_url=page_url,
        )
        if task_index != self._current_task_index:
            return
        self._captured_profile = captured
        self._last_profile_response_url = page_url
        self._last_profile_payload = payload
        self._last_profile_types = profile_types
        self._last_profile_seen_at = datetime.now(UTC)
        self._process_captured_events(trigger="capture:profile")

    def _store_contact_capture(
        self,
        *,
        task_index: int,
        creator_id: str,
        payload: dict[str, object],
        page_url: str,
    ) -> None:
        if self._is_task_finalizing(task_index):
            return
        captured = _CapturedContact(
            creator_id=creator_id,
            payload=payload,
            page_url=page_url,
        )
        if task_index != self._current_task_index:
            return
        self._contact_positive_signal = True
        self._captured_contact = captured
        self._last_contact_response_url = page_url
        self._last_contact_payload = payload
        self._process_captured_events(trigger="capture:contact")

    def _load_cached_captures_for_current_task(self) -> None:
        return

    def _clear_cached_capture_for_task(self, task_index: int) -> None:
        return

    def _is_task_finalizing(self, task_index: int | None) -> bool:
        return task_index is not None and task_index in self._finalizing_task_indexes

    def _begin_task_finalization(self, task_index: int | None) -> None:
        if task_index is None:
            return
        self._finalizing_task_indexes.add(task_index)

    def _finish_task_finalization(self, task_index: int | None) -> None:
        if task_index is None:
            return
        self._finalizing_task_indexes.discard(task_index)

    def _discard_stale_captures(self) -> None:
        if self._captured_profile is not None and not self._is_profile_capture_current(
            self._captured_profile
        ):
            self._log_event(
                "stale_profile_discarded",
                captured_creator_id=self._captured_profile.creator_id,
                captured_page_url=self._captured_profile.page_url,
                attempt_creator_id=self._attempt_creator_id,
                attempt_page_url=self._attempt_page_url,
            )
            self._captured_profile = None
        if self._captured_contact is not None and not self._is_contact_capture_current(
            self._captured_contact
        ):
            self._log_event(
                "stale_contact_discarded",
                captured_creator_id=self._captured_contact.creator_id,
                captured_page_url=self._captured_contact.page_url,
                attempt_creator_id=self._attempt_creator_id,
                attempt_page_url=self._attempt_page_url,
            )
            self._captured_contact = None

    def _is_profile_capture_current(self, captured: _CapturedProfile) -> bool:
        if not self._attempt_creator_id or captured.creator_id != self._attempt_creator_id:
            return False
        return self._captured_page_matches_attempt(captured.page_url)

    def _is_contact_capture_current(self, captured: _CapturedContact) -> bool:
        if not self._attempt_creator_id or captured.creator_id != self._attempt_creator_id:
            return False
        return self._captured_page_matches_attempt(captured.page_url)

    def _captured_page_matches_attempt(self, captured_page_url: str) -> bool:
        captured_page_url = captured_page_url.strip()
        if not captured_page_url or not self._attempt_page_url:
            return True
        return query_param(captured_page_url, "cid") == query_param(self._attempt_page_url, "cid")

    def _apply_profile(self) -> None:
        if self._captured_profile is None or self._current_task_index is None:
            return
        if self._is_task_finalizing(self._current_task_index):
            return
        item = self._items_by_index.get(self._current_task_index)
        if item is None:
            self._advance_to_next_pending(open_page=True)
            return
        creator_id = normalized_creator_id(item.task_data.get("creator_oecuid"))
        captured = self._captured_profile
        self._captured_profile = None
        if captured.creator_id != creator_id:
            self._log_event(
                "profile_ignored",
                task_index=self._current_task_index,
                expected_creator_id=creator_id,
                captured_creator_id=captured.creator_id,
            )
            return
        payload = captured.payload
        response_code = int(payload.get("code") or 0)
        item_region = normalized_region(item.task_data.get("selection_region"))
        request_region = normalized_region(
            captured.shop_region or query_param(self._safe_page_url(), "shop_region")
        )
        self._log_event(
            "profile_apply",
            task_index=self._current_task_index,
            response_code=response_code,
            item_region=item_region,
            request_region=request_region,
            captured_creator_id=captured.creator_id,
        )
        if response_code != 0:
            if self._is_captcha_present():
                self._pause(PAUSE_REASON_CAPTCHA, "检测到人机验证，请先完成验证后继续。")
                self._mark_current_paused(STATE_STATUS_PAUSED_CAPTCHA)
                return
            if item_region and request_region and item_region != request_region:
                self._log_event(
                    "profile_region_mismatch",
                    task_index=self._current_task_index,
                    response_code=response_code,
                    item_region=item_region,
                    request_region=request_region,
                )
                self._pause(
                    PAUSE_REASON_REGION_MISMATCH,
                    self._with_subject("当前店铺区域与达人区域不匹配，请处理后继续。"),
                )
                self._mark_current_paused(STATE_STATUS_PAUSED_REGION_MISMATCH)
                return
            self._handle_retryable_manual_failure(
                "达人资料接口返回异常，请处理页面后继续，或跳过当前达人。"
            )
            return

        if item_region and request_region and item_region != request_region:
            self._log_event(
                "profile_region_mismatch",
                task_index=self._current_task_index,
                response_code=response_code,
                item_region=item_region,
                request_region=request_region,
            )
            self._pause(
                PAUSE_REASON_REGION_MISMATCH,
                self._with_subject("当前店铺区域与达人区域不匹配，请处理后继续。"),
            )
            self._mark_current_paused(STATE_STATUS_PAUSED_REGION_MISMATCH)
            return

        profile = payload.get("creator_profile")
        if not isinstance(profile, dict):
            profile = {}
        patch: dict[str, object] = {}
        bio = nested_value(profile.get("bio"))
        patch["bio"] = bio or "-"
        self._last_contact_available_raw = profile.get("contact_info_available")
        contact_available = contact_info_available(profile.get("contact_info_available"))
        self._last_contact_available = contact_available
        self._profile_patch_applied = True
        if contact_available is not None:
            patch["contact_info_available"] = "true" if contact_available else "false"
        if patch:
            self._app_service.update_task_item_data(
                task_id=self._task_id,
                task_index=self._current_task_index,
                task_data_patch=patch,
            )
            self._refresh_items()

        has_contact = (
            contact_available is True
            or self._contact_positive_signal
            or self._captured_contact is not None
            or self._last_contact_badge_clicked
        )
        self._log_event(
            "profile_contact_state",
            task_index=self._current_task_index,
            contact_available=contact_available,
            contact_available_raw=self._last_contact_available_raw,
            has_contact=has_contact,
            contact_positive_signal=self._contact_positive_signal,
            captured_contact=self._captured_contact is not None,
            badge_clicked=self._last_contact_badge_clicked,
            patch_keys=sorted(patch.keys()),
        )

        if contact_available is False and not has_contact:
            self._log_event("no_contact", task_index=self._current_task_index)
            self._mark_no_contact_and_advance()
            return
        if not has_contact and contact_available is not True:
            self._log_event(
                "profile_contact_state_invalid",
                task_index=self._current_task_index,
                contact_available=contact_available,
                contact_available_raw=self._last_contact_available_raw,
            )
            self._handle_retryable_manual_failure(
                self._with_subject(
                    "达人资料已返回，但 contact_info_available 不是明确的 true/false，请人工处理。"
                )
            )
            return

        self._current_step = "waiting_contact_badge"
        self._waiting_started_at = datetime.now(UTC)
        self._last_message = self._with_subject("有联系方式，正在采集。")
        if self._captured_contact is not None:
            self._current_step = "waiting_contact"
            self._contact_positive_signal = True
            return
        if self._last_contact_badge_clicked:
            self._current_step = "waiting_contact"
            self._waiting_started_at = datetime.now(UTC)
            self._last_message = self._with_subject("有联系方式，正在采集。")
            return

    def _apply_contact(self) -> None:
        if self._captured_contact is None or self._current_task_index is None:
            return
        if self._is_task_finalizing(self._current_task_index):
            return
        item = self._items_by_index.get(self._current_task_index)
        if item is None:
            self._advance_to_next_pending(open_page=True)
            return
        creator_id = normalized_creator_id(item.task_data.get("creator_oecuid"))
        captured = self._captured_contact
        self._captured_contact = None
        if captured.creator_id != creator_id:
            return
        payload = captured.payload
        response_code = int(payload.get("code") or 0)
        if response_code != 0:
            self._log_event(
                "contact_apply_error",
                task_index=self._current_task_index,
                response_code=response_code,
            )
            self._handle_retryable_manual_failure(
                "联系方式接口返回异常，请处理后继续，或跳过当前达人。"
            )
            return
        parsed = contact_patch(payload, self._current_region or "")
        self._log_event(
            "contact_apply",
            task_index=self._current_task_index,
            total_entries=parsed.total_entries,
            valued_entries=parsed.valued_entries,
            recognized_entries=parsed.recognized_entries,
            patch_keys=sorted(parsed.patch.keys()),
        )
        if parsed.patch:
            finalized_task_index = self._current_task_index
            self._begin_task_finalization(finalized_task_index)
            subject = self._current_subject()
            region = self._current_region or ""
            self._app_service.update_task_item_data(
                task_id=self._task_id,
                task_index=self._current_task_index,
                task_data_patch=parsed.patch,
            )
            self._refresh_items()
            update_item_state(
                self._state,
                task_index=finalized_task_index,
                status=STATE_STATUS_SUCCESS,
                region=region,
            )
            self._persist_state()
            self._advance_to_next_pending(open_page=True)
            self._last_message = self._message_for_subject(subject, "已保存补充资料。")
            self._finish_task_finalization(finalized_task_index)
            self._continue_after_terminal_transition()
            return
        if parsed.total_entries <= 0:
            self._handle_retryable_manual_failure(
                self._with_subject(
                    "资料显示存在联系方式，但联系方式接口未返回任何联系方式数据，请人工处理。"
                )
            )
            return
        if parsed.valued_entries <= 0:
            self._handle_retryable_manual_failure(
                self._with_subject("联系方式接口已返回，但所有联系方式值均为空，请人工处理。")
            )
            return
        if parsed.recognized_entries <= 0:
            self._handle_retryable_manual_failure(
                self._with_subject(
                    "联系方式接口已返回，但未解析到可识别的联系方式字段，请人工处理。"
                )
            )
