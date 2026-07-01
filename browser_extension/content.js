(() => {
  const MAX_TEXT = 500;
  const MAX_REQUIREMENTS_TEXT = 12000;
  const MAX_PAGE_TEXT = 60000;
  const MAX_POSTINGS = 80;
  const MAX_LINKEDIN_DETAIL_POSTINGS = 12;
  const LINKEDIN_DETAIL_WAIT_MS = 1100;
  const CAPTURE_COMMAND = "recruit-capture:capture-visible-postings";
  const EXTENSION_VERSION = "0.1.0";

  function cleanText(value, maxLength = MAX_TEXT) {
    return String(value || "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, maxLength);
  }

  function absoluteUrl(value) {
    try {
      return new URL(value, window.location.href).toString();
    } catch (_error) {
      return "";
    }
  }

  function sourceIdForHost(hostname) {
    const host = hostname.toLowerCase();
    if (host.includes("saramin.co.kr")) return "saramin";
    if (host.includes("jobkorea.co.kr")) return "jobkorea";
    if (host.includes("wanted.co.kr")) return "wanted";
    if (host.includes("rallit.com")) return "rallit";
    if (host.includes("rocketpunch.com")) return "rocketpunch";
    if (host.includes("linkedin.com")) return "linkedin";
    return "unknown";
  }

  function isTopWindow() {
    try {
      return window.top === window.self;
    } catch (_error) {
      return false;
    }
  }

  function sourcePostingId(sourceId, url) {
    if (!url) return "";
    const patterns = {
      saramin: [/rec_idx=(\d+)/, /\/(\d+)(?:\?|$)/],
      jobkorea: [/GI_Read\/(\d+)/i, /gi_read\/(\d+)/i],
      linkedin: [/\/jobs\/view\/(\d+)/, /currentJobId=(\d+)/],
      wanted: [/\/wd\/(\d+)/],
      rallit: [/\/positions\/(\d+)/],
      rocketpunch: [/\/jobs\/(\d+)/, /\/jobs\/([^/?#]+)/]
    };
    for (const pattern of patterns[sourceId] || []) {
      const match = url.match(pattern);
      if (match) return match[1];
    }
    return "";
  }

  function looksLikeJobUrl(sourceId, url) {
    if (!url) return false;
    if (sourceId === "saramin") return /\/zf_user\/jobs\/(?:relay\/view|view)|rec_idx=/.test(url);
    if (sourceId === "jobkorea") return /\/Recruit\/GI_Read\//i.test(url);
    if (sourceId === "linkedin") return /\/jobs\/view\//.test(url);
    if (sourceId === "wanted") return /\/wd\/\d+/.test(url);
    if (sourceId === "rallit") return /\/positions\/\d+/.test(url);
    if (sourceId === "rocketpunch") return /\/jobs\//.test(url);
    return /job|career|recruit|position/i.test(url);
  }

  function isCurrentDetailPage(sourceId) {
    const url = absoluteUrl(window.location.href);
    if (sourceId === "saramin") return /\/zf_user\/jobs\/(?:relay\/view|view)|rec_idx=/.test(url);
    if (sourceId === "jobkorea") return /\/Recruit\/GI_Read\//i.test(url);
    if (sourceId === "linkedin") return /\/jobs\/view\/\d+/.test(window.location.pathname);
    if (sourceId === "wanted") return /\/wd\/\d+/.test(window.location.pathname);
    if (sourceId === "rallit") return /\/positions\/\d+/.test(window.location.pathname);
    if (sourceId === "rocketpunch") return /\/jobs\//.test(window.location.pathname);
    return false;
  }

  function nearestCard(anchor) {
    return (
      anchor.closest("li") ||
      anchor.closest("article") ||
      anchor.closest("[data-job-id]") ||
      anchor.closest("[data-occludable-job-id]") ||
      anchor.closest(".job-card-container") ||
      anchor.closest(".list_item") ||
      anchor.closest(".item_recruit") ||
      anchor.closest(".recruit_item") ||
      anchor.parentElement
    );
  }

  function linkedInJobCard(anchor) {
    return (
      anchor.closest("[data-occludable-job-id]") ||
      anchor.closest("[data-job-id]") ||
      anchor.closest(".job-card-container") ||
      anchor.closest("li")
    );
  }

  function textFromSelectors(root, selectors, maxLength = MAX_TEXT) {
    for (const selector of selectors) {
      const el = root.querySelector(selector);
      const text = cleanText(el && el.textContent, maxLength);
      if (text) return text;
    }
    return "";
  }

  function accessibleDocuments() {
    const docs = [document];
    for (const frame of document.querySelectorAll("iframe, frame")) {
      try {
        if (frame.contentDocument && frame.contentDocument.body) {
          docs.push(frame.contentDocument);
        }
      } catch (_error) {
        // Cross-origin frames are intentionally ignored; only visible same-origin
        // posting content can be read without additional permissions.
      }
    }
    return docs;
  }

  function inferCompany(sourceId, card) {
    const value = textFromSelectors(card, {
      saramin: [".corp_name", ".company_nm", "[class*='company']", "[class*='corp']"],
      jobkorea: [".corp", ".name", ".company", "[class*='corp']", "[class*='company']"],
      linkedin: [
        ".job-card-container__primary-description",
        ".base-search-card__subtitle",
        "[class*='company-name']",
        "[class*='subtitle']"
      ]
    }[sourceId] || ["[class*='company']", "[class*='corp']"]);
    return cleanText(value.split("AI 기업소개", 1)[0].replace(/기업정보$/, ""));
  }

  function inferLocation(sourceId, card) {
    const selected = textFromSelectors(card, {
      saramin: [".job_condition", "[class*='condition']", "[class*='location']", "[class*='area']"],
      jobkorea: [".option", ".loc", "[class*='location']", "[class*='area']"],
      linkedin: [
        ".job-card-container__metadata-item",
        ".job-search-card__location",
        ".base-search-card__metadata",
        "[class*='location']"
      ]
    }[sourceId] || ["[class*='location']", "[class*='area']"]);
    return selected || locationFromText(cleanText(card.textContent));
  }

  function locationFromText(text) {
    const labeled = text.match(
      /근무지역\s+((?:서울|경기|인천|부산|대전|대구|광주|울산|세종|강원|충북|충남|전북|전남|경북|경남|제주|Remote|재택)[^\n\r·|,)]{0,30})/
    );
    if (labeled) return cleanText(labeled[1].split(/\s(?:마감일|경력|연봉|직무|고용형태)\s/, 1)[0]);

    const match = text.match(
      /(서울|경기|인천|부산|대전|대구|광주|울산|세종|강원|충북|충남|전북|전남|경북|경남|제주|Remote|재택)[^\s,)\]]{0,12}(?:\s[^\s,)\]]{1,8})?/
    );
    return match ? cleanText(match[0]) : "";
  }

  function inferDeadline(card) {
    const text = cleanText(card.textContent);
    const dateMatch = text.match(/20\d{2}[./-]\d{1,2}[./-]\d{1,2}/);
    if (dateMatch) return dateMatch[0].replace(/[.]/g, "-");
    const shortDateMatch = text.match(/~\s*\d{1,2}[./]\d{1,2}\s*\([^)]*\)/);
    if (shortDateMatch) return cleanText(shortDateMatch[0]);
    const ddayMatch = text.match(/D-\d+|오늘마감|상시채용|채용시|마감임박/);
    return ddayMatch ? ddayMatch[0] : "";
  }

  function inferTags(card) {
    const text = cleanText(card.textContent);
    const tags = [];
    for (const pattern of [/경력\s*\d+\s*년[^\s,]*/g, /신입/g, /경력무관/g, /Python/gi, /SQL/gi, /AI/gi, /LLM/gi, /데이터/g, /머신러닝/g]) {
      for (const match of text.matchAll(pattern)) {
        tags.push(cleanText(match[0]));
      }
    }
    return [...new Set(tags)].slice(0, 20);
  }

  function normalizePosting(sourceId, anchor, detailOverride = "") {
    const card = nearestCard(anchor);
    const sourceUrl = absoluteUrl(anchor.href);
    const postingId = sourcePostingId(sourceId, sourceUrl);
    const title = cleanText(anchor.textContent) || textFromSelectors(card, ["h1", "h2", "h3", "[class*='title']"]);
    const visibleText = cleanText(card.textContent, MAX_REQUIREMENTS_TEXT);
    const detailText = detailOverride || inferDetailRequirements(sourceId, postingId, sourceUrl);
    if (/^(홈페이지 지원|입사지원|기업정보|스크랩|공유하기|지원하기)$/.test(title)) {
      return null;
    }
    return {
      source_id: sourceId,
      source_url: sourceUrl,
      source_posting_id: postingId,
      title,
      company: inferCompany(sourceId, card),
      location: inferLocation(sourceId, card),
      deadline: inferDeadline(card),
      skills: inferTags(card),
      requirements: detailText || visibleText,
      captured_at: new Date().toISOString()
    };
  }

  function normalizePostingSnapshot(sourceId, snapshot, detailText = "") {
    if (/^(홈페이지 지원|입사지원|기업정보|스크랩|공유하기|지원하기)$/.test(snapshot.title)) {
      return null;
    }
    return {
      source_id: sourceId,
      source_url: snapshot.source_url,
      source_posting_id: snapshot.source_posting_id,
      title: snapshot.title,
      company: snapshot.company,
      location: snapshot.location,
      deadline: snapshot.deadline,
      skills: snapshot.skills,
      requirements: detailText || snapshot.visible_text,
      captured_at: new Date().toISOString()
    };
  }

  function inferDetailRequirements(sourceId, postingId, sourceUrl) {
    if (sourceId === "linkedin") {
      return inferLinkedInDetailRequirements(postingId);
    }
    if (sourceId === "saramin") {
      return inferSourceDetailRequirementsSync("saramin");
    }
    if (sourceId === "jobkorea") {
      return inferSourceDetailRequirementsSync("jobkorea");
    }
    if (absoluteUrl(window.location.href).split(/[?#]/, 1)[0] === sourceUrl.split(/[?#]/, 1)[0]) {
      return textFromSelectors(document, [
        "[class*='job_view']",
        "[class*='job-detail']",
        "[class*='job_detail']",
        "[class*='description']",
        "article",
        "main"
      ]);
    }
    return "";
  }

  async function inferSourceDetailRequirements(sourceId, _hints = {}) {
    return inferSourceDetailRequirementsSync(sourceId);
  }


  function inferSourceDetailRequirementsSync(sourceId) {
    const selectorMap = {
      saramin: [
        ".wrap_jv_cont",
        ".jv_cont",
        ".cont_recruit",
        ".job_view_content",
        "[class*='jv_cont']",
        "[class*='job_view']",
        "[class*='recruit_view']",
        "main",
        "article",
        "body"
      ],
      jobkorea: [
        ".artReadJobSum",
        ".tbDetailWrap",
        ".detailView",
        ".recruit-detail",
        ".giRead",
        "[class*='recruit-detail']",
        "[class*='job-detail']",
        "[class*='detail']",
        "main",
        "article",
        "body"
      ]
    };

    for (const doc of accessibleDocuments()) {
      const classBased = sourceDetailTextFromSelectors(doc, selectorMap[sourceId] || ["main", "article"]);
      if (classBased) return cleanSourceDetailText(classBased);
    }

    let bestText = "";
    const markers = accessibleDocuments()
      .flatMap((doc) => [...doc.querySelectorAll("h1, h2, h3, h4, strong, b, span, div, th")])
      .filter((el) => sourceDetailMarkerPattern().test(cleanText(el.textContent, 140)));

    for (const marker of markers) {
      let node = marker;
      for (let depth = 0; node && depth < 9; depth += 1) {
        const text = cleanText(node.innerText || node.textContent, MAX_REQUIREMENTS_TEXT);
        if (looksLikeSourceDetailText(text) && text.length > bestText.length) {
          bestText = text;
        }
        node = node.parentElement;
      }
    }
    if (bestText) return cleanSourceDetailText(bestText);

    const pageText = cleanText(
      accessibleDocuments()
        .map((doc) => doc.body && (doc.body.innerText || doc.body.textContent))
        .join(" "),
      MAX_PAGE_TEXT
    );
    const markerIndex = pageText.search(sourceDetailMarkerPattern());
    if (markerIndex >= 0) {
      const sliced = pageText.slice(markerIndex, markerIndex + MAX_REQUIREMENTS_TEXT);
      if (looksLikeSourceDetailText(sliced)) return cleanSourceDetailText(sliced);
    }
    return "";
  }


  function sourceDetailTextFromSelectors(root, selectors) {
    for (const selector of selectors) {
      const el = root.querySelector(selector);
      const text = cleanText(el && (el.innerText || el.textContent), MAX_REQUIREMENTS_TEXT);
      if (looksLikeSourceDetailText(text)) return text;
    }
    return "";
  }

  function sourceDetailMarkerPattern() {
    return /상세요강|상세 모집 요강|모집부문|모집 분야|담당업무|주요업무|업무내용|이런 업무를 해요|자격요건|지원자격|이런 분들을 찾고 있어요|우대사항|이런 분이면 더 좋아요|근무조건|이런 조건에서 근무할 예정이에요|복리후생|함께하면 이런 점들이 좋아요|전형절차|합류 여정|채용정보|공고상세|포지션|Responsibilities|Requirements|Qualifications|Preferred/i;
  }

  function looksLikeSourceDetailText(text) {
    return text.length > 500 && sourceDetailMarkerPattern().test(text);
  }

  function cleanSourceDetailText(text) {
    return cleanText(text, MAX_REQUIREMENTS_TEXT)
      .replace(/\s*(즉시지원|입사지원|스크랩|관심기업 등록|공유하기|인쇄하기)\s*$/i, "")
      .trim();
  }

  function inferLinkedInDetailRequirements(postingId) {
    const currentJobId = new URLSearchParams(window.location.search).get("currentJobId");
    if (postingId && currentJobId && postingId !== currentJobId) return "";

    const classBased = detailTextFromSelectors(document, [
      ".jobs-description__content",
      ".jobs-box__html-content",
      "[class*='jobs-description']",
      "[class*='job-details'] [class*='description']"
    ]);
    if (looksLikeDetailText(classBased)) return cleanDetailText(classBased);

    let bestText = "";
    const detailMarkerPattern = /채용공고 설명|채용공고 정보|업무 내용|찾는 사람|지원 방법|Job description|About Us|Job Responsibilities|Requirements:/i;
    const markers = [...document.querySelectorAll("h1, h2, h3, h4, span, div")]
      .filter((el) => detailMarkerPattern.test(cleanText(el.textContent, 120)));

    for (const marker of markers) {
      let node = marker;
      for (let depth = 0; node && depth < 8; depth += 1) {
        const text = cleanText(node.innerText || node.textContent, MAX_REQUIREMENTS_TEXT);
        if (looksLikeDetailText(text) && text.length > bestText.length) {
          bestText = text;
        }
        node = node.parentElement;
      }
    }
    if (bestText) return cleanDetailText(bestText);

    const pageText = cleanText(document.body.innerText || document.body.textContent, MAX_PAGE_TEXT);
    const markerIndex = pageText.search(detailMarkerPattern);
    if (markerIndex >= 0) {
      return cleanDetailText(pageText.slice(markerIndex));
    }
    return "";
  }

  function detailTextFromSelectors(root, selectors) {
    for (const selector of selectors) {
      const el = root.querySelector(selector);
      const text = cleanText(el && (el.innerText || el.textContent), MAX_REQUIREMENTS_TEXT);
      if (looksLikeDetailText(text)) return text;
    }
    return "";
  }

  function looksLikeDetailText(text) {
    return (
      text.length > 300 &&
      /채용공고 설명|채용공고 정보|업무 내용|찾는 사람|지원 방법|Job description|About Us|Job Responsibilities|Responsibilities|Requirements|Qualifications/i.test(text)
    );
  }

  function cleanDetailText(text) {
    return cleanText(text, MAX_REQUIREMENTS_TEXT)
      .replace(/\s*(프리미엄으로 목표 빠르게 달성하기|회사 소개|나중에 함께 근무하고 싶으신가요\?).*$/i, "")
      .trim();
  }

  function postingDiagnostics(posting) {
    const requirements = posting && posting.requirements ? posting.requirements : "";
    const manualReviewFlags = [];
    if (posting && posting.source_id === "saramin" && !requirements) {
      manualReviewFlags.push("본문 OCR 필요: 사람인 이미지형 JD 또는 DOM 텍스트 없음");
    }
    return {
      source_posting_id: posting && posting.source_posting_id || "",
      detail_length: requirements.length,
      has_requirements: Boolean(requirements),
      marker_hit: sourceDetailMarkerPattern().test(requirements),
      manual_review_flags: manualReviewFlags
    };
  }

  function withCaptureDiagnostics(payload, extra = {}) {
    const postings = Array.isArray(payload.postings) ? payload.postings : [];
    return {
      ...payload,
      extension_version: EXTENSION_VERSION,
      diagnostics: {
        schema_version: payload.capture_schema_version,
        extension_version: EXTENSION_VERSION,
        source_id: payload.source_id,
        capture_mode: payload.capture_mode || "visible_list",
        page_url: payload.page_url,
        posting_count: postings.length,
        postings: postings.map(postingDiagnostics),
        warnings: [],
        errors: [],
        ...extra
      }
    };
  }


  function captureVisiblePostingsSync() {
    const sourceId = sourceIdForHost(window.location.hostname);
    const anchors = [...document.querySelectorAll("a[href]")]
      .filter((anchor) => looksLikeJobUrl(sourceId, absoluteUrl(anchor.href)));
    const seen = new Set();
    const postings = [];
    for (const anchor of anchors) {
      const posting = normalizePosting(sourceId, anchor);
      if (!posting) continue;
      const key = posting.source_url || `${posting.title}|${posting.company}`;
      if (!posting.title || seen.has(key)) continue;
      seen.add(key);
      postings.push(posting);
      if (postings.length >= MAX_POSTINGS) break;
    }
    return withCaptureDiagnostics({
      capture_schema_version: 1,
      capture_mode: "visible_list",
      source_id: sourceId,
      page_url: window.location.href,
      page_title: document.title,
      captured_at: new Date().toISOString(),
      postings
    }, {
      extraction_strategy: "visible_anchor_list"
    });
  }

  async function captureVisiblePostings() {
    const sourceId = sourceIdForHost(window.location.hostname);
    if (["saramin", "jobkorea", "wanted", "rallit", "rocketpunch"].includes(sourceId) && isCurrentDetailPage(sourceId)) {
      return captureCurrentDetailPosting(sourceId);
    }
    if (sourceId === "linkedin") {
      return captureLinkedInVisiblePostings();
    }
    return captureVisiblePostingsSync();
  }

  async function captureCurrentDetailPosting(sourceId) {
    const sourceUrl = window.location.href;
    const postingId = sourcePostingId(sourceId, sourceUrl);
    const main = document.querySelector("main") || document.body;
    const pageRoot = document.body || main;
    const title = textFromSelectors(main, {
      saramin: [".tit_job", ".job_tit", ".jv_header h1", "h1", "h2"],
      jobkorea: [".hd_3 .tit", ".sumTit", ".artReadJobSum h3", ".tbDetailWrap h2", "h1", "h2"]
    }[sourceId] || ["h1", "h2"]) || cleanText(document.title.replace(/\s*[-|ㅣ]\s*(사람인|잡코리아).*$/i, ""));
    const company = textFromSelectors(pageRoot, {
      saramin: [".corp_name", ".jv_header .company", ".company", "[class*='corp']", "[class*='company']"],
      jobkorea: ["a[href*='/Recruit/Co_Read/']", ".coName", ".corp", ".company", "[class*='corp']", "[class*='company']"]
    }[sourceId] || ["[class*='corp']", "[class*='company']"]);
    const detailText = await inferSourceDetailRequirements(sourceId, { title, company });
    let resolvedCompany = company;
    if (sourceId === "wanted") {
      const wantedCompany = detailText.match(/^(.{1,80}?)∙/);
      if (wantedCompany) resolvedCompany = cleanText(wantedCompany[1]);
    }
    if (sourceId === "rallit") {
      const rallitCompany = detailText.match(/회사명\s+(.{1,80}?)\s+(?:\d+\s+)?지원하기/);
      if (rallitCompany) resolvedCompany = cleanText(rallitCompany[1]);
    }
    const posting = {
      source_id: sourceId,
      source_url: sourceUrl,
      source_posting_id: postingId,
      title,
      company: resolvedCompany,
      location: locationFromText(cleanText(pageRoot.innerText || pageRoot.textContent, 12000)),
      deadline: inferDeadline(main),
      skills: inferTags(main),
      requirements: detailText,
      captured_at: new Date().toISOString()
    };
    return withCaptureDiagnostics({
      capture_schema_version: 1,
      capture_mode: "current_detail",
      source_id: sourceId,
      page_url: window.location.href,
      page_title: document.title,
      captured_at: new Date().toISOString(),
      postings: posting.title && posting.requirements ? [posting] : []
    }, {
      extraction_strategy: "current_detail_dom",
      iframe_status: "same_origin_dom_only"
    });
  }

  function linkedInAnchorSnapshot(anchor) {
    const card = linkedInJobCard(anchor);
    if (!card) return null;
    const sourceUrl = absoluteUrl(anchor.href);
    const postingId = sourcePostingId("linkedin", sourceUrl);
    const title = cleanText(anchor.textContent) || textFromSelectors(card, ["h1", "h2", "h3", "[class*='title']"]);
    return {
      element: anchor,
      source_url: sourceUrl,
      source_posting_id: postingId,
      title,
      company: inferCompany("linkedin", card),
      location: inferLocation("linkedin", card),
      deadline: inferDeadline(card),
      skills: inferTags(card),
      visible_text: cleanText(card.textContent, MAX_REQUIREMENTS_TEXT)
    };
  }

  async function captureLinkedInVisiblePostings() {
    if (isCurrentDetailPage("linkedin")) {
      return captureLinkedInCurrentDetailPosting();
    }

    const anchors = [...document.querySelectorAll("a[href]")]
      .filter((anchor) => looksLikeJobUrl("linkedin", absoluteUrl(anchor.href)));
    const seen = new Set();
    const snapshots = [];
    for (const anchor of anchors) {
      const snapshot = linkedInAnchorSnapshot(anchor);
      if (!snapshot) continue;
      const key = snapshot.source_posting_id || snapshot.source_url || `${snapshot.title}|${snapshot.company}`;
      if (!snapshot.title || seen.has(key)) continue;
      seen.add(key);
      snapshots.push(snapshot);
      if (snapshots.length >= MAX_LINKEDIN_DETAIL_POSTINGS) break;
    }

    const postings = [];
    for (const snapshot of snapshots) {
      await selectLinkedInPosting(snapshot);
      const detailText = inferLinkedInDetailRequirements(snapshot.source_posting_id);
      const posting = normalizePostingSnapshot("linkedin", snapshot, detailText);
      if (posting) postings.push(posting);
    }

    return withCaptureDiagnostics({
      capture_schema_version: 1,
      capture_mode: "visible_detail_clickthrough",
      source_id: "linkedin",
      page_url: window.location.href,
      page_title: document.title,
      captured_at: new Date().toISOString(),
      postings
    }, {
      extraction_strategy: "linkedin_visible_detail_clickthrough",
      clickthrough: {
        attempted: snapshots.length,
        succeeded: postings.length,
        failed: Math.max(0, snapshots.length - postings.length)
      }
    });
  }

  function captureLinkedInCurrentDetailPosting() {
    const sourceUrl = window.location.href;
    const postingId = sourcePostingId("linkedin", sourceUrl);
    const detailText = inferLinkedInDetailRequirements(postingId);
    const main = document.querySelector("main") || document.body;
    const title = textFromSelectors(main, [
      "h1",
      ".job-details-jobs-unified-top-card__job-title",
      "[class*='top-card'] [class*='title']"
    ]) || cleanText(document.title.replace(/\s*\|\s*LinkedIn.*$/i, ""));
    const company = textFromSelectors(main, [
      "a[href*='/company/']",
      ".job-details-jobs-unified-top-card__company-name",
      "[class*='company-name']"
    ]);
    const location = locationFromText(cleanText(main.innerText || main.textContent, 2000));
    const posting = {
      source_id: "linkedin",
      source_url: sourceUrl,
      source_posting_id: postingId,
      title,
      company,
      location,
      deadline: "",
      skills: inferTags(main),
      requirements: detailText,
      captured_at: new Date().toISOString()
    };
    return withCaptureDiagnostics({
      capture_schema_version: 1,
      capture_mode: "current_detail",
      source_id: "linkedin",
      page_url: window.location.href,
      page_title: document.title,
      captured_at: new Date().toISOString(),
      postings: posting.title && posting.requirements ? [posting] : []
    }, {
      extraction_strategy: "linkedin_current_detail_dom"
    });
  }

  async function selectLinkedInPosting(snapshot) {
    const beforeUrl = window.location.href;
    snapshot.element.scrollIntoView({ block: "center", inline: "nearest" });
    snapshot.element.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      view: window
    }));
    await waitForLinkedInDetail(snapshot.source_posting_id, snapshot.title, beforeUrl);
  }

  async function waitForLinkedInDetail(postingId, title, beforeUrl) {
    const start = Date.now();
    while (Date.now() - start < 5000) {
      const currentJobId = new URLSearchParams(window.location.search).get("currentJobId");
      const detailText = inferLinkedInDetailRequirements(postingId);
      const urlChanged = window.location.href !== beforeUrl;
      const idMatched = postingId && currentJobId === postingId;
      const titleMatched = title && cleanText(document.body.innerText || document.body.textContent, MAX_PAGE_TEXT).includes(title);
      if (detailText && (idMatched || titleMatched || urlChanged)) return;
      await sleep(200);
    }
    await sleep(LINKEDIN_DETAIL_WAIT_MS);
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function filenameForPayload(payload) {
    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    const sourceId = cleanText(payload.source_id || "unknown").toLowerCase().replace(/[^a-z0-9_-]+/g, "-") || "unknown";
    return `recruit-captures/${new Date().toISOString().slice(0, 10)}/${sourceId}/recruit-capture-${sourceId}-${timestamp}.json`;
  }

  async function downloadPayloadFromPage(payload) {
    if (chrome && chrome.runtime && chrome.runtime.sendMessage) {
      const response = await chrome.runtime.sendMessage({
        type: "recruit-capture:download",
        payload
      });
      if (response && response.ok) return response;
      if (response && response.error) throw new Error(response.error);
    }

    const filename = filenameForPayload(payload);
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json"
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.style.display = "none";
    document.documentElement.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    return { filename };
  }

  async function handleExplicitCaptureCommand(source = "unknown", options = {}) {
    const payload = await captureVisiblePostings();
    const shouldDownload = options.download !== false;
    const download = shouldDownload ? await downloadPayloadFromPage(payload) : { requested: false };
    return {
      ok: true,
      source,
      payload,
      validation: {
        posting_count: Array.isArray(payload.postings) ? payload.postings.length : 0
      },
      download: {
        requested: shouldDownload,
        ...(download || {})
      }
    };
  }


  function registerCaptureCommandHandler() {
    if (!isTopWindow()) return;
    if (window.__recruitCaptureCommandHandlerRegistered) return;
    window.__recruitCaptureCommandHandlerRegistered = true;
    chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (!message || message.type !== CAPTURE_COMMAND) return false;
      handleExplicitCaptureCommand(message.source || "popup", message.options || {})
        .then(sendResponse)
        .catch((error) => sendResponse({
          ok: false,
          error: error && error.message ? error.message : String(error)
        }));
      return true;
    });
  }


  function injectCaptureButton() {
    if (!isTopWindow() || document.getElementById("recruit-capture-floating-button")) return;
    const sourceId = sourceIdForHost(window.location.hostname);
    if (!["saramin", "jobkorea", "linkedin", "wanted", "rallit", "rocketpunch"].includes(sourceId)) return;

    const button = document.createElement("button");
    button.id = "recruit-capture-floating-button";
    button.type = "button";
    button.textContent = "Recruit Capture";
    button.title = "Save visible recruiting postings as JSON";
    Object.assign(button.style, {
      position: "fixed",
      right: "18px",
      bottom: "18px",
      zIndex: "2147483647",
      border: "0",
      borderRadius: "6px",
      padding: "10px 12px",
      background: "#0a66c2",
      color: "#fff",
      font: "600 13px/1.2 -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
      boxShadow: "0 6px 18px rgba(0, 0, 0, 0.22)",
      cursor: "pointer"
    });
    button.addEventListener("click", async () => {
      const label = button.textContent;
      button.disabled = true;
      button.textContent = "Capturing...";
      try {
        const result = await handleExplicitCaptureCommand("floating_button", { download: true });
        button.textContent = `Saved ${result.validation.posting_count}`;
      } catch (error) {
        button.textContent = error && error.message ? error.message.slice(0, 28) : "Capture failed";
      } finally {
        window.setTimeout(() => {
        button.textContent = label;
        button.disabled = false;
        }, 1800);
      }
    });
    document.documentElement.append(button);

    window.addEventListener("keydown", (event) => {
      if (event.altKey && event.shiftKey && event.code === "KeyR") {
        event.preventDefault();
        button.click();
      }
    });
  }

  registerCaptureCommandHandler();

  injectCaptureButton();
})();
