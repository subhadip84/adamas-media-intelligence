

/* =========================================================
   API CONFIGURATION
========================================================= */

const API = window.location.hostname === "localhost"
    ? "http://localhost:8000"
    : "https://adamas-media-intelligence.onrender.com";


/* =========================================================
   USER KEY
========================================================= */

// Keep the anonymous/free-search identity separate from the paid
// subscriber account identity. A subscriber logout must return the browser
// to its own free-search quota rather than reusing the subscriber's account
// user_key.
let userKey =
    localStorage.getItem("ami_anon_user_key");

if(!userKey){
    // One-time migration from the older key name. The old value may have
    // been overwritten by a subscriber login in earlier versions, so only
    // reuse it when an anonymous key already exists. Otherwise create a new
    // anonymous identity.
    userKey =
        "user-" +
        crypto.randomUUID();

    localStorage.setItem(
        "ami_anon_user_key",
        userKey
    );
}


let adminKey =
    sessionStorage.getItem(
        "ami_admin_key"
    );

let subscriberToken =
    sessionStorage.getItem(
        "ami_subscriber_token"
    ) || "";

let subscriberEmail =
    sessionStorage.getItem(
        "ami_subscriber_email"
    ) || "";


let currentResults = [];
let savedSearchesCache = [];

// Google-style result pagination: 10 articles per page.
// currentResults remains the complete search result set so existing
// save-search, compare, filters and intelligence functionality are preserved.
let resultsCurrentPage = 1;
const RESULTS_PER_PAGE = 10;

function isPaidSubscriber(){ return Boolean(subscriberToken && subscriberSessionValid); }
function savedFilterSignature(filters){
  const f=filters||{};
  return JSON.stringify({date_range:String(f.date_range||"30"),categories:String(f.categories||""),language:String(f.language||""),source:String(f.source||"")});
}
function searchSignature(query,filters){
  return JSON.stringify({query:String(query||"").trim().toLowerCase(),filters:savedFilterSignature(filters)});
}
function isArticleSearchSaved(query){
  const target=String(query||"").trim().toLowerCase();
  if(!target || !isPaidSubscriber()) return false;
  return savedSearchesCache.some(x => String(x.query||"").trim().toLowerCase() === target);
}

function refreshArticleSaveButtons(){
  document.querySelectorAll('.article-save-search[data-article-id]').forEach(btn=>{
    const article=currentResults.find(x=>Number(x.id)===Number(btn.dataset.articleId));
    if(!article) return;
    const saved=isArticleSearchSaved(article.title);
    if(!isPaidSubscriber()){
      btn.disabled=true;
      btn.textContent='🔒 Save Search (Subscribers)';
      btn.title='Save Search is available to paid subscribers only';
      btn.classList.remove('saved-search-active');
    }else{
      btn.disabled=false;
      btn.textContent=saved ? '✓ Saved Search' : '☆ Save Search';
      btn.title=saved ? 'This article search is saved for future use' : 'Save this article search for present and future use';
      btn.classList.toggle('saved-search-active', saved);
    }
  });
}

function updateSaveSearchAccess(){
  const top=document.getElementById("saveSearchTopButton");
  if(!top)return;
  const canSave=isPaidSubscriber() && Boolean(lastSearchQuery) && currentResults.length>0;
  top.disabled=!canSave;
  const exactSaved=savedSearchesCache.some(x=>searchSignature(x.query,x.filters)===searchSignature(lastSearchQuery,Object.fromEntries(getSearchFilterParams().entries())));
  top.textContent=canSave ? (exactSaved ? "✓ Saved Search" : "☆ Save Search") : "🔒 Save Search (Subscribers)";
  top.title=isPaidSubscriber() ? (canSave ? (exactSaved ? "This search is saved for future use" : "Save this search for present and future use") : "Run a search with results first") : "Save Search is available to paid subscribers only";
  refreshArticleSaveButtons();
}


/* =========================================================
   SUBSCRIBER DISPLAY
========================================================= */

function getSubscriberDisplayName(email){
    const value = String(email || "").trim().toLowerCase();
    if(!value) return "Subscriber";

    // Build the welcome name generically from the email local-part.
    // Digits are ignored so addresses such as:
    //   subhadip.bagchi1@...      -> Subhadip Bagchi
    //   subhadip1.bagchi2026@...  -> Subhadip Bagchi
    //   john2.doe7@...            -> John Doe
    // The subscriber email/account identity itself is never changed.
    const localPart = value.split("@")[0] || "Subscriber";
    const words = localPart
        .replace(/[0-9]+/g, "")
        .replace(/[._-]+/g, " ")
        .replace(/\s+/g, " ")
        .trim()
        .split(" ")
        .filter(Boolean);

    if(!words.length) return "Subscriber";

    return words.map(word =>
        word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()
    ).join(" ");
}

function updateSubscriberWelcome(isPremium){
    const subscribeCard = document.getElementById("subscribeCard");
    const welcomeCard = document.getElementById("subscriberWelcomeCard");
    const welcomeTitle = document.getElementById("subscriberWelcomeTitle");

    if(!subscribeCard || !welcomeCard) return;

    if(isPremium && subscriberToken){
        const name = getSubscriberDisplayName(subscriberEmail);
        if(welcomeTitle){
            welcomeTitle.textContent = "Welcome, " + name;
        }
        subscribeCard.style.display = "none";
        welcomeCard.style.display = "block";
    }else{
        subscribeCard.style.display = "block";
        welcomeCard.style.display = "none";
    }
}


/* =========================================================
   QUOTA
========================================================= */

async function refreshQuota(){

    try{

        const response =
            await fetch(

                API + "/api/quota",

                {
                    headers:{
                        "X-User-Key":userKey,
                        "X-Auth-Token":subscriberToken
                    }
                }

            );


        if(!response.ok){

            throw new Error();

        }


        const data =
            await response.json();

        // Backend is authoritative for subscriber state.
        subscriberSessionValid = Boolean(
            subscriberToken && data.subscribed === true
        );

        // The server is authoritative. Show the welcome card only when the
        // current subscriber token is actually valid and premium access is
        // active. This also prevents a stale/expired token from leaving the
        // paid UI visible after logout or token expiry.
        if(data.subscribed && subscriberToken){
            updateSubscriberWelcome(true);
        }else{
            updateSubscriberWelcome(false);
        }


        const remaining =
            data.remaining ?? 0;


        const limit =
            data.free_limit ?? 4;


        const used =
            data.subscribed
                ? null
                : Math.max(
                    0,
                    limit - remaining
                );


        document
            .getElementById("quotaCircle")
            .textContent =
                used === null
                    ? "∞"
                    : used + " / " + limit;


        document
            .getElementById("quotaText")
            .textContent =
                data.subscribed
                    ? "Premium access enabled"
                    : (
                        "Free searches used. " +
                        remaining +
                        " free search" +
                        (remaining === 1 ? "" : "es") +
                        " remaining."
                    );

        updateSaveSearchAccess();

    }

    catch(error){

        subscriberSessionValid = false;

        document
            .getElementById("quotaCircle")
            .textContent = "-";


        document
            .getElementById("quotaText")
            .textContent =
                "Quota information unavailable.";

    }

}



/* =========================================================
   GOOGLE-STYLE SEARCH AUTOCOMPLETE
   ========================================================= */

/* The autocomplete serves two search boxes: the hero search (#searchInput)
   and the frozen results search bar (#amiSearchResultsDockInput), which
   index.html clones into #amiSearchResultsDock after a search. All functions
   below work on the currently active input + dropdown pair. */
const heroSuggestionBox =
    document.getElementById("searchSuggestions");
let suggestionBox = heroSuggestionBox;
let suggestionInput = null;

function activeSuggestionInput(){
    return suggestionInput || searchInput;
}

function useSuggestionTarget(input, box){
    if(suggestionInput === input && suggestionBox === box) return;
    // Close the dropdown of the previously used search box.
    if(suggestionBox && suggestionBox !== box){
        suggestionBox.classList.remove("show");
        suggestionBox.innerHTML = "";
    }
    if(suggestionInput && suggestionInput !== input){
        suggestionInput.setAttribute("aria-expanded", "false");
        suggestionInput.removeAttribute("aria-activedescendant");
    }
    suggestionInput = input;
    suggestionBox = box;
    suggestionIndex = -1;
}

let suggestionTimer = null;
let suggestionController = null;
let suggestionIndex = -1;

function escapeSuggestionText(value){
    return escapeHtml(String(value || ""));
}

function highlightSuggestion(value, query){
    const safe = escapeSuggestionText(value);
    const q = String(query || "").trim();
    if(!q){
        return safe;
    }

    // Highlight the typed text when it appears literally.
    const escapedQ = escapeSuggestionText(q)
        .replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

    if(!escapedQ){
        return safe;
    }

    try{
        return safe.replace(
            new RegExp(`(${escapedQ})`, "ig"),
            '<span class="match">$1</span>'
        );
    }
    catch(error){
        return safe;
    }
}

function hideSuggestions(){
    suggestionBox.classList.remove("show");
    suggestionBox.innerHTML = "";
    suggestionIndex = -1;
    activeSuggestionInput().setAttribute("aria-expanded", "false");
}

function setActiveSuggestion(index){
    const items = suggestionBox.querySelectorAll(".search-suggestion");
    if(!items.length){ suggestionIndex=-1; return; }
    suggestionIndex = Math.max(-1, Math.min(index, items.length - 1));
    items.forEach((item, i) => {
        const active = i === suggestionIndex;
        item.classList.toggle("active", active);
        item.setAttribute("aria-selected", active ? "true" : "false");
    });
    if(suggestionIndex >= 0){
        const active = items[suggestionIndex];
        activeSuggestionInput().setAttribute("aria-activedescendant", active.id);
        const activeText = active.querySelector(".search-suggestion-text");
        prefetchResults(activeText ? activeText.textContent : active.textContent);
        active.scrollIntoView({block:"nearest"});
    } else {
        activeSuggestionInput().removeAttribute("aria-activedescendant");
    }
}

async function selectSearchSuggestion(value){
    const selected = String(value || "").trim();
    if(!selected) return;
    const typedInput = activeSuggestionInput();
    searchInput.value = selected;
    frozenSearchInputs().forEach(function(input){ input.value = selected; });
    typedInput.setSelectionRange(selected.length, selected.length);
    hideSuggestions();
    await performSearch(selected);
}

function showSuggestions(items, query){
    if(!items || !items.length){
        hideSuggestions();
        return;
    }

    suggestionBox.innerHTML = items.map((item, index) => `
        <button
            type="button"
            class="search-suggestion"
            role="option"
            id="searchSuggestion-${index}"
            aria-selected="false"
            data-index="${index}"
        >
            <span class="search-suggestion-icon" aria-hidden="true">⌕</span>
            <span class="search-suggestion-text">${highlightSuggestion(item, query)}</span>
        </button>
    `).join("");

    suggestionIndex = -1;
    suggestionBox.classList.add("show");
    activeSuggestionInput().setAttribute("aria-expanded", "true");
    activeSuggestionInput().removeAttribute("aria-activedescendant");

    suggestionBox.querySelectorAll(".search-suggestion").forEach(button => {
        button.addEventListener("mouseenter", () => {
            setActiveSuggestion(Number(button.dataset.index));
        });
        button.addEventListener("mousedown", event => event.preventDefault());
        button.addEventListener("click", async () => {
            const value = button.querySelector(".search-suggestion-text").textContent.trim();
            await selectSearchSuggestion(value);
        });
    });
}

/* Instant predictions (Google-style):
   - every server answer is remembered in the browser, so typing the same
     prefix again, or backspacing, shows predictions with no network wait;
   - while the server is asked about a longer prefix, the predictions of the
     previous prefix are filtered locally and shown immediately. */
const suggestionCache = new Map();
const SUGGESTION_CACHE_MAX = 300;
let lastShownSuggestionKey = "";

function suggestionKey(query){
    return String(query || "").trim().toLowerCase().replace(/\s+/g, " ");
}

function cachedSuggestions(query){
    return suggestionCache.get(suggestionKey(query));
}

function rememberSuggestions(query, items){
    const key = suggestionKey(query);
    suggestionCache.delete(key);
    suggestionCache.set(key, items);
    if(suggestionCache.size > SUGGESTION_CACHE_MAX){
        suggestionCache.delete(suggestionCache.keys().next().value);
    }
}

function instantSuggestions(query){
    const key = suggestionKey(query);
    for(let len = key.length - 1; len >= 2; len--){
        const previous = suggestionCache.get(key.slice(0, len));
        if(previous){
            return previous.filter(item => String(item).toLowerCase().includes(key));
        }
    }
    return null;
}

/* ---------------------------------------------------------
   Local prediction index: popular searches, headline words/phrases,
   sources and categories, downloaded once (cached 5 minutes). Predictions
   are computed in the browser on every keystroke with no network wait;
   the server suggestions endpoint then refines the list.
--------------------------------------------------------- */
let predictionIndex = [];

async function loadPredictionIndex(){
    try{
        const response = await fetch(API + "/api/search/prediction-index", {headers:{"Accept":"application/json"}});
        if(!response.ok) return;
        const data = await response.json();
        predictionIndex = (Array.isArray(data.items) ? data.items : [])
            .map(item => ({text: String(item[0]), lower: String(item[0]).toLowerCase(), weight: Number(item[1]) || 0}));
    }catch(error){ /* predictions still come from the server */ }
}

function localPredictions(query){
    const key = suggestionKey(query);
    if(!key || !predictionIndex.length) return [];
    const scored = [];
    for(const item of predictionIndex){
        let score;
        if(item.lower === key) score = 5000;
        else if(item.lower.startsWith(key)) score = 3000;
        else if(item.lower.includes(" " + key)) score = 1500;
        else continue;
        scored.push([score + Math.min(item.weight, 2000), item.text]);
    }
    scored.sort((a, b) => b[0] - a[0]);
    return scored.slice(0, 10).map(item => item[1]);
}

/* ---------------------------------------------------------
   Result prefetch: while the user types or highlights a prediction, the
   results are fetched in the background from /api/search/preview (no quota
   used). Clicking/pressing Enter then shows them instantly; the normal
   /api/search call still runs to count the search.
--------------------------------------------------------- */
const resultPrefetch = new Map();
const RESULT_PREFETCH_MAX = 40;
const RESULT_PREFETCH_TTL_MS = 55000;
let prefetchTimer = null;

function resultPrefetchKey(query){
    return suggestionKey(query) + "|" + getSearchFilterParams().toString();
}

function freshPrefetch(query){
    const entry = resultPrefetch.get(resultPrefetchKey(query));
    if(!entry || Date.now() - entry.at > RESULT_PREFETCH_TTL_MS) return null;
    return entry;
}

function prefetchResults(query){
    const text = String(query || "").trim();
    if(text.length < 2 || freshPrefetch(text)) return;
    const key = resultPrefetchKey(text);
    const entry = {at: Date.now(), data: null, promise: null};
    entry.promise = fetch(
        API + "/api/search/preview?q=" + encodeURIComponent(text) + "&" + getSearchFilterParams().toString(),
        {headers:{"X-User-Key": userKey, "X-Auth-Token": subscriberToken}}
    )
        .then(response => response.ok ? response.json() : null)
        .then(data => {
            if(data && Array.isArray(data.results)) entry.data = data;
            else resultPrefetch.delete(key);
            return entry.data;
        })
        .catch(() => { resultPrefetch.delete(key); return null; });
    resultPrefetch.set(key, entry);
    while(resultPrefetch.size > RESULT_PREFETCH_MAX){
        resultPrefetch.delete(resultPrefetch.keys().next().value);
    }
}

function schedulePrefetch(query, items){
    clearTimeout(prefetchTimer);
    prefetchTimer = setTimeout(() => {
        prefetchResults(query);
        if(items && items.length) prefetchResults(items[0]);
    }, 250);
}

function renderSuggestionsOnce(items, query){
    // Avoid re-rendering (and losing the arrow-key highlight) when the same
    // list for this query is already on screen.
    const signature = suggestionKey(query) + "\u0000" + items.join("\u0001");
    if(signature === lastShownSuggestionKey && suggestionBox.classList.contains("show")) return;
    lastShownSuggestionKey = signature;
    showSuggestions(items, query);
    schedulePrefetch(query, items);
}

async function loadSuggestions(query){
    if(!query || query.length < 2){
        hideSuggestions();
        return;
    }
    const cached = cachedSuggestions(query);
    if(cached){
        if(suggestionController) suggestionController.abort();
        renderSuggestionsOnce(cached, query);
        return;
    }
    if(suggestionController) suggestionController.abort();
    suggestionController = new AbortController();
    try{
        const response = await fetch(API + "/api/search/suggestions?q=" + encodeURIComponent(query), {
            signal: suggestionController.signal,
            headers:{"Accept":"application/json"}
        });
        if(!response.ok){ hideSuggestions(); return; }
        const data = await response.json();
        const suggestions = Array.isArray(data.suggestions) ? data.suggestions : [];
        rememberSuggestions(query, suggestions);
        // Ignore a late answer for text the user has already changed.
        if(suggestionKey(activeSuggestionInput().value) !== suggestionKey(query)) return;
        renderSuggestionsOnce(suggestions, query);
    }catch(error){
        if(error.name !== "AbortError") hideSuggestions();
    }
}

const searchInput = document.getElementById("searchInput");

function bindSearchAutocomplete(input, box){

input.addEventListener("input", function(){
    useSuggestionTarget(input, box);
    const value = this.value.trim();
    clearTimeout(suggestionTimer);
    if(!value){ hideSuggestions(); return; }
    // Known prefix: show immediately, no debounce and no network request.
    if(value.length >= 2 && cachedSuggestions(value)){
        loadSuggestions(value);
        return;
    }
    // New prefix: filter the previous predictions locally right away, then
    // ask the server after a short pause in typing.
    let instant = value.length >= 2 ? instantSuggestions(value) : null;
    if(!instant || !instant.length) instant = localPredictions(value);
    if(instant && instant.length) renderSuggestionsOnce(instant, value);
    else if(value.length < 2) hideSuggestions();
    // The server refines predictions from 2 characters.
    if(value.length < 2) return;
    suggestionTimer = setTimeout(() => loadSuggestions(value), 100);
});

if(!box.dataset.amiHoverPrefetch){
    box.dataset.amiHoverPrefetch = "1";
    box.addEventListener("mouseover", function(event){
        const option = event.target.closest(".search-suggestion");
        if(!option) return;
        const text = option.querySelector(".search-suggestion-text");
        prefetchResults(text ? text.textContent : option.textContent);
    });
}

input.addEventListener("keydown", async function(event){
    useSuggestionTarget(input, box);
    const items = suggestionBox.querySelectorAll(".search-suggestion");
    const open = suggestionBox.classList.contains("show") && items.length;

    if(event.key === "ArrowDown" && open){
        event.preventDefault();
        setActiveSuggestion(suggestionIndex + 1);
        return;
    }
    if(event.key === "ArrowUp" && open){
        event.preventDefault();
        setActiveSuggestion(suggestionIndex - 1);
        return;
    }
    if(event.key === "Home" && open && (event.ctrlKey || event.metaKey)){
        event.preventDefault();
        setActiveSuggestion(0);
        return;
    }
    if(event.key === "End" && open && (event.ctrlKey || event.metaKey)){
        event.preventDefault();
        setActiveSuggestion(items.length - 1);
        return;
    }
    if(event.key === "Escape"){
        if(open){ event.preventDefault(); hideSuggestions(); }
        return;
    }
    // Tab accepts the highlighted prediction and immediately searches it.
    if(event.key === "Tab" && open){
        // Tab/Shift+Tab can also choose a visible prediction. If nothing is
        // highlighted yet, Tab accepts the first suggestion; Shift+Tab accepts
        // the last suggestion. The chosen value is placed in the search box
        // and the exact selected phrase is searched immediately.
        event.preventDefault();
        const index = suggestionIndex >= 0
            ? suggestionIndex
            : (event.shiftKey ? items.length - 1 : 0);
        setActiveSuggestion(index);
        const value = items[index].querySelector(".search-suggestion-text").textContent.trim();
        await selectSearchSuggestion(value);
        return;
    }
    if(event.key === "Enter" && open && suggestionIndex >= 0){
        event.preventDefault();
        const value = items[suggestionIndex].querySelector(".search-suggestion-text").textContent.trim();
        await selectSearchSuggestion(value);
        return;
    }
});

}

bindSearchAutocomplete(searchInput, heroSuggestionBox);

document.addEventListener("click", function(event){
    const input = activeSuggestionInput();
    if(!input.contains(event.target) && !suggestionBox.contains(event.target)) hideSuggestions();
});

/* Frozen search bar autocomplete.
   index.html creates frozen copies of the search form after a search:
     #amiSearchResultsDock (input #amiSearchResultsDockInput) and
     #amiSearchFreezeBar   (input #amiSearchFreezeInput, v9 script).
   Those copies have no prediction dropdown (the v9 script even removes it)
   and no autocomplete listeners. When a frozen bar appears, give it its own
   dropdown directly under the bar and attach the same autocomplete. */
const FROZEN_SEARCH_BARS = [
    {bar: "amiSearchResultsDock", input: "amiSearchResultsDockInput"},
    {bar: "amiSearchFreezeBar", input: "amiSearchFreezeInput"},
];

function frozenSearchInputs(){
    return FROZEN_SEARCH_BARS
        .map(cfg => document.getElementById(cfg.input))
        .filter(Boolean);
}

function attachFrozenBarAutocomplete(){
    FROZEN_SEARCH_BARS.forEach(function(cfg){
        const input = document.getElementById(cfg.input);
        if(!input || input.dataset.amiAutocomplete === "1") return;
        const form = input.closest("form");
        if(!form) return;
        input.dataset.amiAutocomplete = "1";

        const holder = document.createElement("div");
        holder.className = "ami-frozen-suggest-wrap";
        const box = document.createElement("div");
        box.id = cfg.input + "Suggestions";
        box.className = "search-suggestions ami-frozen-suggestions";
        box.setAttribute("role", "listbox");
        box.setAttribute("aria-label", "Search predictions");
        holder.appendChild(box);
        form.insertAdjacentElement("afterend", holder);

        input.setAttribute("role", "combobox");
        input.setAttribute("aria-autocomplete", "list");
        input.setAttribute("aria-controls", box.id);
        input.setAttribute("aria-expanded", "false");
        input.setAttribute("autocomplete", "off");

        bindSearchAutocomplete(input, box);
    });
}

new MutationObserver(attachFrozenBarAutocomplete)
    .observe(document.body, {childList: true, subtree: true});
attachFrozenBarAutocomplete();

// Close a frozen bar's dropdown when that bar is hidden (scrolled back up).
window.addEventListener("scroll", function(){
    if(suggestionBox === heroSuggestionBox) return;
    const bar = suggestionBox.closest("#amiSearchResultsDock, #amiSearchFreezeBar");
    if(!bar || window.getComputedStyle(bar).display === "none") hideSuggestions();
}, {passive: true});



function amiSyncStickySearchTop(){
    const header = document.querySelector(".topbar");
    const h = header ? Math.ceil(header.getBoundingClientRect().height) : 76;
    document.documentElement.style.setProperty("--ami-sticky-search-top", h + "px");
    if(window.amiSyncStickySearchHeight) window.amiSyncStickySearchHeight();
}

function amiSyncStickySearchTop(){
    const header = document.querySelector(".topbar");
    const h = header ? Math.ceil(header.getBoundingClientRect().height) : 76;
    document.documentElement.style.setProperty("--ami-sticky-search-top", h + "px");
}

function activateGoogleStickySearch(){
    const wrap = document.querySelector(".search-panel-wrap");
    const header = document.querySelector(".topbar");
    if(!wrap || !header) return;

    /* Keep an exact DOM marker so the search can be restored to the hero. */
    if(!window.__amiStickySearchMarker){
        const marker = document.createComment("AMI_SEARCH_ORIGINAL_POSITION");
        wrap.parentNode.insertBefore(marker, wrap);
        window.__amiStickySearchMarker = marker;
    }

    /* Put the sticky bar immediately after the header. Because it remains in
       normal document flow, KPI cards and results can never pass underneath it. */
    header.parentNode.insertBefore(wrap, header.nextSibling);
    wrap.classList.add("ami-search-sticky");
    document.body.classList.add("ami-search-active");
    amiSyncStickySearchTop();
}

function deactivateGoogleStickySearch(){
    const wrap = document.querySelector(".search-panel-wrap");
    const marker = window.__amiStickySearchMarker;

    if(wrap && marker && marker.parentNode){
        marker.parentNode.insertBefore(wrap, marker.nextSibling);
        marker.remove();
        window.__amiStickySearchMarker = null;
    }else if(wrap){
        wrap.classList.remove("ami-search-sticky");
    }

    if(wrap) wrap.classList.remove("ami-search-sticky");
    document.body.classList.remove("ami-search-active");
}


/* =========================================================
   SEARCH
========================================================= */

document
    .getElementById("searchForm")
    .addEventListener(

        "submit",

        async function(event){

            event.preventDefault();

            const query =
                document
                    .getElementById("searchInput")
                    .value
                    .trim();


            if(!query){

                return;

            }


            await performSearch(query);

        }

    );


async function quickSearch(query){

    hideSuggestions();

    document
        .getElementById("searchInput")
        .value = query;


    await performSearch(query);

}


function getSearchFilterParams(){
    const params = new URLSearchParams();

    const dateRange = document.getElementById("dateRange");
    if(dateRange && dateRange.value){
        params.set("date_range", dateRange.value);
    }

    const selectedCategories = getSelectedCategories();
    if(selectedCategories.length){
        params.set("categories", selectedCategories.join(","));
    }

    const language = document.getElementById("languageFilter");
    if(language && language.value){
        params.set("language", language.value);
    }

    const source = document.getElementById("sourceFilter");
    if(source && source.value){
        params.set("source", source.value);
    }

    return params;
}

let lastSearchQuery = "";

async function performSearch(query, filterOnly = false){

    const resultsList =
        document
            .getElementById("resultsList");


    const resultsTitle =
        document
            .getElementById("resultsTitle");


    lastSearchQuery = String(query || "").trim();
    const searchedQuery = lastSearchQuery;

    // Instant results: use results prefetched while the user was typing.
    const prefetched = filterOnly ? null : freshPrefetch(searchedQuery);
    let shownFromPrefetch = false;
    let realSearchDone = false;
    let shownResultIds = "";

    if(prefetched && prefetched.data){
        applySearchData(prefetched.data, true);
        shownFromPrefetch = true;
        shownResultIds = resultIds(prefetched.data);
    }else{
        resultsTitle.textContent =
            "Searching...";

        resultsList.innerHTML =
            '<div class="loading">Searching indexed media sources...</div>';

        // A prefetch still in flight may answer before the real request.
        if(prefetched && prefetched.promise){
            prefetched.promise.then(data => {
                if(data && !realSearchDone && lastSearchQuery === searchedQuery){
                    applySearchData(data, true);
                    shownFromPrefetch = true;
                    shownResultIds = resultIds(data);
                }
            });
        }
    }


    try{

        const response =
            await fetch(

                API +
                "/api/search?q=" +
                encodeURIComponent(query) +
                "&" +
                getSearchFilterParams().toString(),

                {
                    headers:{
                        "X-User-Key":userKey,
                        "X-Auth-Token":subscriberToken
                    }
                }

            );


        realSearchDone = true;

        if(response.status === 402){

            if(shownFromPrefetch){
                currentResults = [];
                resultsTitle.textContent = "Search Results";
                resultsList.innerHTML = "";
            }

            openQuotaModal();

            await updateSubscriberButton();

refreshQuota();

            return;

        }


        if(!response.ok){

            throw new Error(
                "Search request failed"
            );

        }


        const data =
            await response.json();

        // Already on screen from the prefetch: only refresh if different.
        if(shownFromPrefetch && resultIds(data) === shownResultIds){
            refreshQuota();
            return;
        }

        applySearchData(data, !shownFromPrefetch);

    }

    catch(error){

        if(shownFromPrefetch) return;

        resultsTitle.textContent =
            "Search Results";


        const saveTop = document.getElementById("saveSearchTopButton");
        if(saveTop) saveTop.disabled = true;

        resultsList.innerHTML =
            `
            <div class="empty-state">
                Unable to connect to the Media Intelligence API.
                Please ensure the Docker services are running.
            </div>
            `;

    }

}


function resultIds(data){
    return (Array.isArray(data && data.results) ? data.results : [])
        .map(item => item.id).join(",");
}


/* Render a search response (from /api/search or a prefetched preview). */
function applySearchData(data, scroll){

        currentResults =
            Array.isArray(data.results)
                ? data.results
                : [];

        // Every new search starts from page 1, like a search-engine results page.
        resultsCurrentPage = 1;

        const saveTop = document.getElementById("saveSearchTopButton");
        if(saveTop) saveTop.disabled = !isPaidSubscriber() || !lastSearchQuery || currentResults.length === 0;
        updateSaveSearchAccess();


        populateSourceFilter();

        // The backend has already applied the selected filters. Do not apply
        // them a second time on the client because semantic filters (for
        // example Education) may intentionally match title/summary while the
        // stored category is "National" or another generic feed category.
        const resultsTitle = document.getElementById("resultsTitle");
        resultsTitle.textContent =
            "Search Results (" + currentResults.length + ")";
        renderResults(currentResults);

        // Google-style behaviour: once a search is submitted successfully,
        // keep the search bar accessible at the top while the results scroll.
        activateGoogleStickySearch();

        // Quota refresh no longer blocks showing the results.
        refreshQuota();

        if(scroll) scrollToResults();

}


/* =========================================================
   RENDER RESULTS
========================================================= */

function renderResults(results){

    const container = document.getElementById("resultsList");
    if(!container) return;

    // Keep the complete result set in memory; only the visible slice is rendered.
    const allResults = Array.isArray(results) ? results : [];
    const total = allResults.length;
    const totalPages = Math.max(1, Math.ceil(total / RESULTS_PER_PAGE));

    if(resultsCurrentPage > totalPages) resultsCurrentPage = totalPages;
    if(resultsCurrentPage < 1) resultsCurrentPage = 1;

    // Remove any old pagination generated by a previous render.
    const oldPagination = document.getElementById("amiResultsPagination");
    if(oldPagination) oldPagination.remove();
    const oldPageInfo = document.getElementById("amiResultsPageInfo");
    if(oldPageInfo) oldPageInfo.remove();

    if(!total){

        container.innerHTML =
            `
            <div class="empty-state">
                No indexed result found for this search.
            </div>
            `;

        return;

    }

    const startIndex = (resultsCurrentPage - 1) * RESULTS_PER_PAGE;
    const endIndex = Math.min(startIndex + RESULTS_PER_PAGE, total);
    const pageResults = allResults.slice(startIndex, endIndex);

    container.innerHTML =
        pageResults
            .map(renderArticleCard)
            .join("");

    // Graceful image fallback: never expose broken-image icons or raw HTML.
    container.querySelectorAll('img[data-image-fallback="true"]').forEach(img => {
        img.addEventListener('error', () => {
            const box = img.parentElement;
            if (!box || box.dataset.fallbackApplied === '1') return;
            box.dataset.fallbackApplied = '1';
            box.classList.add('is-fallback');
            box.innerHTML = `
                <div class="no-image-preview" role="img" aria-label="No image preview">
                    <div>
                        <div class="no-image-icon">▧</div>
                        <div class="no-image-text">No image preview</div>
                    </div>
                </div>
            `;
        }, { once:true });
    });

    renderResultsPagination(total);

    // Search-engine style relevance cue: identify the strongest result returned
    // by the backend without changing the user's result set.
    const relevanceHint = document.getElementById("amiRelevanceHint");
    if(relevanceHint) relevanceHint.remove();

    if(allResults.length && allResults[0].is_most_relevant){
        const hint = document.createElement("div");
        hint.id = "amiRelevanceHint";
        hint.style.cssText = "text-align:center;color:#71839a;font-size:12px;margin:4px 0 12px;";
        hint.innerHTML = '🎯 <strong style="color:#805f08">Most Relevant</strong> — ranked highest for your search';
        const nav = document.getElementById("amiResultsPagination");
        if(nav) nav.parentNode.insertBefore(hint, nav);
        else container.insertAdjacentElement("afterend", hint);
    }
}

function renderResultsPagination(total){

    const resultsList = document.getElementById("resultsList");
    if(!resultsList || !total) return;

    const totalPages = Math.ceil(total / RESULTS_PER_PAGE);
    if(totalPages <= 1) return;

    // Google-like compact page navigation.
    const nav = document.createElement("nav");
    nav.id = "amiResultsPagination";
    nav.className = "ami-results-pagination";
    nav.setAttribute("aria-label", "Search result pages");

    const addButton = (label, page, options={}) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "ami-page-btn" + (options.active ? " active" : "");
        button.textContent = label;
        button.setAttribute("aria-label", options.aria || ("Page " + page));
        if(options.active) button.setAttribute("aria-current", "page");
        if(options.disabled) button.disabled = true;
        if(!options.disabled){
            button.addEventListener("click", () => goToResultsPage(page));
        }
        nav.appendChild(button);
    };

    addButton("‹", Math.max(1, resultsCurrentPage - 1), {
        disabled: resultsCurrentPage === 1,
        aria: "Previous page"
    });

    const pages = buildResultPageNumbers(totalPages, resultsCurrentPage);

    pages.forEach(page => {
        if(page === "..."){
            const span = document.createElement("span");
            span.className = "ami-results-page-ellipsis";
            span.textContent = "…";
            nav.appendChild(span);
        }else{
            addButton(String(page), page, {
                active: page === resultsCurrentPage
            });
        }
    });

    addButton("›", Math.min(totalPages, resultsCurrentPage + 1), {
        disabled: resultsCurrentPage === totalPages,
        aria: "Next page"
    });

    const info = document.createElement("div");
    info.id = "amiResultsPageInfo";
    info.className = "ami-results-page-info";

    const first = (resultsCurrentPage - 1) * RESULTS_PER_PAGE + 1;
    const last = Math.min(resultsCurrentPage * RESULTS_PER_PAGE, total);

    info.textContent = `Showing ${first}–${last} of ${total} results`;

    // Place navigation immediately after the result cards.
    resultsList.insertAdjacentElement("afterend", info);
    info.insertAdjacentElement("afterend", nav);
}

function buildResultPageNumbers(totalPages, currentPage){

    // Show all pages for small result sets.
    if(totalPages <= 7){
        return Array.from({length: totalPages}, (_, i) => i + 1);
    }

    // Compact Google-style pagination for larger result sets.
    const pages = [1];

    if(currentPage > 4) pages.push("...");

    const start = Math.max(2, currentPage - 1);
    const end = Math.min(totalPages - 1, currentPage + 1);

    for(let page = start; page <= end; page++){
        if(!pages.includes(page)) pages.push(page);
    }

    if(currentPage < totalPages - 3) pages.push("...");

    pages.push(totalPages);

    return pages;
}

function goToResultsPage(page){

    const totalPages = Math.max(
        1,
        Math.ceil(currentResults.length / RESULTS_PER_PAGE)
    );

    const target = Math.max(
        1,
        Math.min(Number(page) || 1, totalPages)
    );

    if(target === resultsCurrentPage) return;

    resultsCurrentPage = target;
    renderResults(currentResults);

    // Keep the result heading/card area in view after changing page.
    const resultsList = document.getElementById("resultsList");
    if(resultsList){
        const top = resultsList.getBoundingClientRect().top + window.scrollY - 90;
        window.scrollTo({top: Math.max(0, top), behavior: "smooth"});
    }
}

function renderArticleCard(article){
    const title = escapeHtml(article.title || "Untitled Article");
    const summary = escapeHtml(cleanArticleText(article.summary || "No summary available."));
    const source = escapeHtml(article.source || "Unknown Source");
    const category = escapeHtml(article.category || "News");
    let dateText = "Date unavailable";
    if(article.published_at){ try{ dateText = new Date(article.published_at).toLocaleDateString(); }catch(error){} }
    const resolvedImageUrl = article.image_url ? article.image_url : `${API}/api/articles/${article.id}/image`;
    const saved = isArticleSearchSaved(article.title);
    const mostRelevant = Boolean(article.is_most_relevant);
    const relevanceBadge = mostRelevant
        ? '<div class="ami-most-relevant-badge" title="Highest relevance for your search"><span class="ami-relevance-icon">🎯</span> Most Relevant</div>'
        : '';
    return `
    <article class="article-card v5-article-card ${mostRelevant ? 'is-most-relevant' : ''}" data-article-id="${Number(article.id)||0}">
      <div class="v5-select-wrap"><input type="checkbox" class="v5-compare-check" data-article-id="${Number(article.id)||0}" aria-label="Select article for comparison"></div>
      <div class="article-image v5-article-image">
        <img src="${escapeAttribute(resolvedImageUrl)}" alt="" loading="lazy" data-image-fallback="true">
      </div>
      <div class="article-content">
        ${relevanceBadge}
        <div class="v5-article-meta"><span class="v5-category-pill">${category}</span><span>${dateText}</span></div>
        <div class="article-title">${title}</div>
        <div class="article-summary">${summary}</div>
        <div class="article-footer"><span class="source-name">${source}</span><span>•</span><span>${dateText}</span></div>
        <div class="v5-article-actions">
          <button type="button" class="v5-btn v5-primary" data-quick-view="${Number(article.id)||0}">👁 Quick View</button>
          <a class="v5-btn v5-outline" href="${escapeAttribute(article.url || '#')}" target="_blank" rel="noopener">Read Original →</a>
          <button type="button" class="v5-btn v5-ghost article-track-topic" data-article-id="${Number(article.id)||0}">✦ Track Topic</button>
          <button type="button" class="v5-btn v5-ghost article-save-search ${saved ? 'saved-search-active' : ''}" data-article-id="${Number(article.id)||0}" ${isPaidSubscriber() ? "" : "disabled"}>${isPaidSubscriber() ? (saved ? "✓ Saved Search" : "☆ Save Search") : "🔒 Save Search"}</button>
        </div>
      </div>
    </article>`;
}


/* =========================================================
   SOURCE FILTER
========================================================= */

function populateSourceFilter(){
    const select = document.getElementById("sourceFilter");
    const current = select.value;
    const existing = new Set();

    currentResults.forEach(item => {
        if(item.source){
            existing.add(item.source);
        }
    });

    const options = Array.from(existing).sort((a,b) =>
        a.localeCompare(b)
    );

    // Only populate from results when the metadata endpoint has not already
    // supplied the full source list.
    if(select.options.length <= 1 && options.length){
        select.innerHTML =
            '<option value="">All Sources</option>' +
            options.map(source =>
                '<option value="' + escapeAttribute(source) + '">' +
                escapeHtml(source) +
                '</option>'
            ).join("");
    }

    if(current){
        select.value = current;
    }
}


/* =========================================================
   SEARCH FILTERS
========================================================= */

function normaliseFilterText(value){
    return String(value || "")
        .toLowerCase()
        .replace(/&/g, "and")
        .replace(/[^a-z0-9]+/g, " ")
        .replace(/\s+/g, " ")
        .trim();
}

function getSelectedCategories(){
    const boxes = Array.from(
        document.querySelectorAll(".categoryFilter")
    );

    const allBox = boxes[0];
    if(allBox && allBox.checked){
        return [];
    }

    return boxes
        .slice(1)
        .filter(box => box.checked)
        .map(box => box.value);
}

function categoryMatches(articleCategory, selectedCategory){
    const actual = normaliseFilterText(articleCategory);
    const wanted = normaliseFilterText(selectedCategory);

    if(!actual || !wanted){
        return false;
    }

    // The UI uses compact names while feeds may store their full labels.
    const aliases = {
        "government": ["government", "government policy", "policy"],
        "research": ["research", "research innovation", "innovation"],
        "campus": ["campus", "campus news"],
        "education": ["education"],
        "technology": ["technology", "tech"],
        "business": ["business"],
        "national": ["national"]
    };

    const candidates = aliases[wanted] || [wanted];
    return candidates.some(candidate =>
        actual === candidate ||
        actual.includes(candidate) ||
        candidate.includes(actual)
    );
}

function getFilteredResults(){
    let results = [...currentResults];

    const dateRange =
        document.getElementById("dateRange").value;

    if(dateRange !== "all"){
        const days = Number(dateRange);
        if(Number.isFinite(days)){
            const cutoff = Date.now() - (days * 24 * 60 * 60 * 1000);
            results = results.filter(article => {
                if(!article.published_at){
                    return false;
                }
                const timestamp = new Date(article.published_at).getTime();
                return Number.isFinite(timestamp) && timestamp >= cutoff;
            });
        }
    }

    const selectedCategories = getSelectedCategories();
    if(selectedCategories.length){
        results = results.filter(article =>
            selectedCategories.some(category =>
                categoryMatches(article.category, category)
            )
        );
    }

    const language =
        document.getElementById("languageFilter").value;
    if(language){
        const wanted = normaliseFilterText(language);
        results = results.filter(article =>
            normaliseFilterText(article.language) === wanted
        );
    }

    const source =
        document.getElementById("sourceFilter").value;
    if(source){
        results = results.filter(article =>
            article.source === source
        );
    }

    return results;
}

function applyFilters(){
    const filtered = getFilteredResults();
    const resultsTitle = document.getElementById("resultsTitle");
    resultsTitle.textContent =
        filtered.length === currentResults.length
            ? "Search Results (" + currentResults.length + ")"
            : "Filtered Results (" + filtered.length + " of " + currentResults.length + ")";
    renderResults(filtered);
}

function loadFilterOptions(){
    fetch(API + "/api/search/filter-options")
        .then(response => {
            if(!response.ok){
                throw new Error("Unable to load filter options");
            }
            return response.json();
        })
        .then(data => {
            const sourceSelect = document.getElementById("sourceFilter");
            const sources = Array.isArray(data.sources) ? data.sources : [];
            const current = sourceSelect.value;
            sourceSelect.innerHTML =
                '<option value="">All Sources</option>' +
                sources.map(source =>
                    '<option value="' + escapeAttribute(source) + '">' +
                    escapeHtml(source) +
                    '</option>'
                ).join("");
            if(sources.includes(current)){
                sourceSelect.value = current;
            }

            // Add languages discovered in the database while retaining the
            // common choices already visible in the UI.
            const languageSelect = document.getElementById("languageFilter");
            const knownLanguages = new Set(
                Array.from(languageSelect.options).map(option => option.value).filter(Boolean)
            );
            (Array.isArray(data.languages) ? data.languages : []).forEach(language => {
                if(language && !knownLanguages.has(language)){
                    const option = document.createElement("option");
                    option.value = language;
                    option.textContent = language;
                    languageSelect.appendChild(option);
                    knownLanguages.add(language);
                }
            });
        })
        .catch(() => {
            // Keep the built-in filter choices if the metadata endpoint is unavailable.
        });
}

function wireSearchFilters(){
    const dateRange = document.getElementById("dateRange");
    const language = document.getElementById("languageFilter");
    const source = document.getElementById("sourceFilter");

    [dateRange, language, source].forEach(control => {
        control.addEventListener("change", function(){
            if(lastSearchQuery){
                performSearch(lastSearchQuery, true);
            }
        });
    });

    document.querySelectorAll(".categoryFilter").forEach(box => {
        box.addEventListener("change", function(){
            const boxes = Array.from(document.querySelectorAll(".categoryFilter"));
            const allBox = boxes[0];

            if(this === allBox && allBox.checked){
                boxes.slice(1).forEach(item => item.checked = false);
            } else if(this !== allBox && this.checked){
                allBox.checked = false;
            }

            if(!boxes.slice(1).some(item => item.checked)){
                allBox.checked = true;
            }

            if(lastSearchQuery){
                performSearch(lastSearchQuery, true);
            }
        });
    });
}

/* =========================================================
   SORT
========================================================= */

function sortResults(){

    const mode =
        document
            .getElementById("sortSelect")
            .value;


    let results =
        [...currentResults];


    if(mode === "newest"){

        results.sort(

            (a,b) => {

                const dateA =
                    new Date(
                        a.published_at || 0
                    );


                const dateB =
                    new Date(
                        b.published_at || 0
                    );


                return dateB - dateA;

            }

        );

    }


    renderResults(results);

}


/* =========================================================
   FILTER RESET
========================================================= */

function resetFilters(){
    document.getElementById("dateRange").value = "30";
    document.getElementById("languageFilter").value = "";
    document.getElementById("sourceFilter").value = "";

    document.querySelectorAll(".categoryFilter").forEach((checkbox, index) => {
        checkbox.checked = index === 0;
    });

    if(lastSearchQuery){
        performSearch(lastSearchQuery, true);
    }
}


/* =========================================================
   ADMIN LOGIN
========================================================= */


function openSubscriberLogin(){
    document.getElementById("subscriberLoginModal").classList.add("show");
    document.getElementById("subscriberEmailInput").focus();
}

function closeSubscriberLogin(){
    document.getElementById("subscriberLoginModal").classList.remove("show");
    document.getElementById("subscriberLoginError").textContent = "";
}

function setDemoSubscriberLoading(loading){
    const ids = [
        "amiKpiMode",
        "amiKpiModeMeta",
        "amiKpiAccess",
        "amiKpiAccessMeta",
        "quotaCircle",
        "quotaText"
    ];

    if(loading){
        const mode = document.getElementById("amiKpiMode");
        const modeMeta = document.getElementById("amiKpiModeMeta");
        const access = document.getElementById("amiKpiAccess");
        const accessMeta = document.getElementById("amiKpiAccessMeta");
        const quotaCircle = document.getElementById("quotaCircle");
        const quotaText = document.getElementById("quotaText");

        if(mode) mode.textContent = "Loading...";
        if(modeMeta) modeMeta.textContent = "Updating subscriber access...";
        if(access) access.textContent = "Loading...";
        if(accessMeta) accessMeta.textContent = "Loading subscriber intelligence...";
        if(quotaCircle) quotaCircle.textContent = "...";
        if(quotaText) quotaText.textContent = "Loading subscriber search usage...";
    }
}

async function subscriberLogin(){
    const email = document.getElementById("subscriberEmailInput").value.trim();
    const password = document.getElementById("subscriberPasswordInput").value;

    const error = document.getElementById("subscriberLoginError");
    error.textContent = "";

    if(!email || !password){
        error.textContent = "Enter your subscription email and password.";
        return;
    }

    setDemoSubscriberLoading(true);

    try{
        const response = await fetch(API + "/api/auth/login", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({email, password})
        });

        let data = {};
        try { data = await response.json(); } catch(e) {}

        if(!response.ok){
            throw new Error(data.detail || "Subscriber sign in failed.");
        }

        subscriberToken = data.token || "";
        subscriberEmail = data.email || email;
        subscriberSessionValid = true;

        // Immediate lightweight UI update; do not wait for quota/intelligence.
        updateSubscriberButton();
        updateSubscriberWelcome(true);
        updateSaveSearchAccess();

        const modeEl = document.getElementById("amiKpiMode");
        const modeMetaEl = document.getElementById("amiKpiModeMeta");
        const accessEl = document.getElementById("amiKpiAccess");
        const accessMetaEl = document.getElementById("amiKpiAccessMeta");
        if(modeEl) modeEl.textContent = "Unlimited";
        if(modeMetaEl) modeMetaEl.textContent = "Premium search access active";
        if(accessEl) accessEl.textContent = "Unlimited";
        if(accessMetaEl) accessMetaEl.textContent = "Premium subscriber access";

        sessionStorage.setItem("ami_subscriber_token", subscriberToken);
        sessionStorage.setItem("ami_subscriber_email", subscriberEmail);

        // IMPORTANT: do not replace the anonymous/free-search user key with
        // the subscriber account key. The backend uses X-Auth-Token to grant
        // premium access; X-User-Key remains the browser's anonymous identity.

        closeSubscriberLogin();
        updateSubscriberButton();
        updateSubscriberWelcome(true);
        await refreshQuota();
        await loadSavedSearches(true);
        updateSaveSearchAccess();
        if(currentResults.length) renderResults(currentResults);

        setDemoSubscriberLoading(false);

        const displayName = getSubscriberDisplayName(subscriberEmail);
        showAppAlert(`Subscriber “${displayName}” sign in successful. Premium search access is active.`, {
            type: "success",
            title: "Adamas Media Intelligence",
            icon: "ℹ",
            okText: "OK"
        });

    }catch(error){
        setDemoSubscriberLoading(false);

        error = error && error.message ? error.message : "Subscriber sign in failed.";
        document.getElementById("subscriberLoginError").textContent = error;
    }
}

function clearSearchResultsOnLogout(){
    // Subscriber search results are session UI state. Once the subscriber
    // signs out, do not leave the previous premium/search context visible.
    currentResults = [];
    lastSearchQuery = "";

    const searchInput = document.getElementById("searchInput");
    if(searchInput) searchInput.value = "";

    const resultsTitle = document.getElementById("resultsTitle");
    if(resultsTitle) resultsTitle.textContent = "Search Results";

    const resultsList = document.getElementById("resultsList");
    if(resultsList){
        resultsList.innerHTML = `
            <div class="empty-state">
                Sign in as a subscriber to search the media intelligence database.
            </div>
        `;
    }

    const saveTop = document.getElementById("saveSearchTopButton");
    if(saveTop) saveTop.disabled = true;

    // Return filters to their neutral state as well.
    const dateRange = document.getElementById("dateRange");
    if(dateRange) dateRange.value = "30";
    const languageFilter = document.getElementById("languageFilter");
    if(languageFilter) languageFilter.value = "";
    const sourceFilter = document.getElementById("sourceFilter");
    if(sourceFilter) sourceFilter.value = "";
    document.querySelectorAll(".categoryFilter").forEach((checkbox, index) => {
        checkbox.checked = index === 0;
    });
}

async function subscriberLogout(){
    const confirmed = await showAmiLogoutConfirm();
    if(!confirmed){
        return;
    }

    // Capture the token before clearing local state.
    const token = subscriberToken;

    // =========================================================
    // INSTANT UI RESET
    // =========================================================
    // Clear the authentication state first so every UI component
    // immediately sees the user as logged out.
    subscriberToken = "";
    subscriberEmail = "";
    subscriberSessionValid = false;
    savedSearchesCache = [];

    try{
        sessionStorage.removeItem("ami_subscriber_token");
        sessionStorage.removeItem("ami_subscriber_email");
        sessionStorage.removeItem("ami_subscriber_user_key");
    }catch(e){}

    // Update the visible subscriber indicators immediately.
    updateSubscriberButton();
    updateSubscriberWelcome(false);
    updateSaveSearchAccess();

    const modeEl = document.getElementById("amiKpiMode");
    const modeMetaEl = document.getElementById("amiKpiModeMeta");
    const accessEl = document.getElementById("amiKpiAccess");
    const accessMetaEl = document.getElementById("amiKpiAccessMeta");
    const quotaCircle = document.getElementById("quotaCircle");
    const quotaText = document.getElementById("quotaText");

    if(modeEl) modeEl.textContent = "Standard";
    if(modeMetaEl) modeMetaEl.textContent = "Free search access";
    if(accessEl) accessEl.textContent = "Free";
    if(accessMetaEl) accessMetaEl.textContent = "Sign in for unlimited search";
    if(quotaCircle) quotaCircle.textContent = "...";
    if(quotaText) quotaText.textContent = "Updating free search usage...";

    // Close the modal immediately after the user's decision.
    const logoutModal = document.getElementById("amiLogoutConfirmModal");
    if(logoutModal){
        logoutModal.classList.remove("show");
        logoutModal.setAttribute("aria-hidden","true");
    }

    // =========================================================
    // BACKGROUND CLEANUP
    // =========================================================
    // Do not make the user wait for these operations.
    setTimeout(function(){
        try{
            if(typeof clearSearchResultsOnLogout === "function"){
                clearSearchResultsOnLogout();
            }
        }catch(e){}

        // Refresh the authoritative free quota in the background.
        try{
            if(typeof refreshQuota === "function"){
                refreshQuota().catch(()=>{});
            }
        }catch(e){}

        // Revoke the server session in the background.
        try{
            if(token){
                fetch(API + "/api/auth/logout", {
                    method:"POST",
                    headers:{"X-Auth-Token":token},
                    keepalive:true
                }).catch(()=>{});
            }
        }catch(e){}
    }, 0);
}

function updatePublicAdminVisibility(){
    const adminButton = document.getElementById("publicAdminButton");
    if(!adminButton) return;

    // Subscriber and Admin are separate access modes. While a paid
    // subscriber session is active, keep the public Admin option hidden.
    // It becomes visible again immediately after subscriber logout.
    adminButton.style.display = subscriberToken ? "none" : "";
}

function updateSubscriberButton(){
    const button = document.getElementById("signInButton");
    if(!button) return;

    updatePublicAdminVisibility();

    if(subscriberToken){
        button.textContent = "Logout";
        button.title = subscriberEmail
            ? "Sign out subscriber: " + subscriberEmail
            : "Sign out paid subscriber";
        button.onclick = subscriberLogout;
    }else{
        button.textContent = "Sign In";
        button.title = "Paid subscriber sign in";
        button.onclick = openSubscriberLogin;
    }
}


function openAdminLogin(){

    document
        .getElementById("adminLoginError")
        .textContent = "";


    document
        .getElementById("adminKeyInput")
        .value = "";


    document
        .getElementById("adminLoginModal")
        .classList.add("show");

}


function closeAdminLogin(){

    document
        .getElementById("adminLoginModal")
        .classList.remove("show");

}


async function adminLogin(){

    const key =
        document
            .getElementById("adminKeyInput")
            .value
            .trim();


    if(!key){

        document
            .getElementById("adminLoginError")
            .textContent =
                "Please enter the Admin Key.";

        return;

    }


    adminKey = key;


    sessionStorage.setItem(
        "ami_admin_key",
        adminKey
    );


    closeAdminLogin();

    openAdminDashboard();

}


/* =========================================================
   ADMIN DASHBOARD
========================================================= */

function openAdminDashboard(){

    document
        .getElementById("publicApp")
        .classList.add("hidden");


    document
        .getElementById("adminDashboard")
        .classList.add("show");


    document
        .getElementById("currentUserKey")
        .textContent =
            userKey;


    loadAdminDashboard();

}


function adminLogout(){

    sessionStorage.removeItem(
        "ami_admin_key"
    );


    adminKey = null;


    document
        .getElementById("adminDashboard")
        .classList.remove("show");


    document
        .getElementById("publicApp")
        .classList.remove("hidden");

}


function showAdminSection(
    section,
    button
){

    document
        .querySelectorAll(
            ".admin-section"
        )
        .forEach(

            element => {

                element.style.display =
                    "none";

            }

        );


    document
        .getElementById(
            "admin-" +
            section +
            "-section"
        )
        .style.display =
            "block";


    document
        .querySelectorAll(
            ".admin-menu-button"
        )
        .forEach(

            element => {

                element.classList.remove(
                    "active"
                );

            }

        );


    if(button){

        button.classList.add(
            "active"
        );

    }


    if(section === "sources"){

        loadSources();

    }


    if(section === "feeds"){

        loadFeeds();

    }


    if(section === "dashboard"){

        loadAdminDashboard();

    }


    if(section === "epaper"){

        loadEpaperPublishers();
        loadSavedEpapers();

    }


    if(section === "quota"){

        loadSearchLimitSetting();
        loadAdminSubscribers();
        loadAutoRssDiscoverySetting();

    }

}


/* =========================================================
   ADMIN API HEADERS
========================================================= */

function adminHeaders(){

    return {

        "X-Admin-Key":
            adminKey ||

            sessionStorage.getItem(
                "ami_admin_key"
            ) ||

            ""

    };

}


/* =========================================================
   BULK SOURCE / RSS FEED IMPORT
========================================================= */

let currentBulkImportType = "sources";

const bulkImportConfig = {
    sources: {
        title: "Bulk Import — Source Management",
        text: "Upload multiple media sources at once. Use the template to keep the column names consistent.",
        columns: ["source_code", "name", "website", "rss_url", "language", "coverage", "active", "decision"],
        templateUrl: "/api/admin/sources/template",
        importUrl: "/api/admin/sources/bulk-import"
    },
    feeds: {
        title: "Bulk Import — RSS Feed Management",
        text: "Upload multiple RSS feeds at once. source_code must already exist in Source Management.",
        columns: ["source_code", "feed_name", "feed_url", "category", "language", "active", "decision"],
        templateUrl: "/api/admin/feeds/template",
        importUrl: "/api/admin/feeds/bulk-import"
    }
};

function openBulkImport(type){
    if(!bulkImportConfig[type]) return;
    currentBulkImportType = type;
    const config = bulkImportConfig[type];
    document.getElementById("bulkImportTitle").textContent = config.title;
    document.getElementById("bulkImportText").textContent = config.text;
    document.getElementById("bulkImportColumns").innerHTML = config.columns.map(c => `<code style="display:inline-block;background:#edf2f7;padding:3px 6px;border-radius:5px;margin:2px 4px 2px 0">${escapeHtml(c)}</code>`).join("");
    document.getElementById("bulkImportFile").value = "";
    const result = document.getElementById("bulkImportResult");
    result.style.display = "none";
    result.innerHTML = "";
    document.getElementById("bulkImportModal").classList.add("show");
}

function closeBulkImport(){
    document.getElementById("bulkImportModal").classList.remove("show");
}

async function downloadBulkTemplate(type){
    const config = bulkImportConfig[type || currentBulkImportType];
    if(!config) return;
    try{
        const response = await fetch(API + config.templateUrl, {headers:adminHeaders()});
        if(!response.ok){
            const data = await response.json().catch(() => ({}));
            throw new Error(data.detail || "Unable to download template.");
        }
        const blob = await response.blob();
        const disposition = response.headers.get("Content-Disposition") || "";
        const match = disposition.match(/filename="?([^";]+)"?/i);
        const filename = match ? match[1] : (type === "feeds" ? "rss_feed_management_template.csv" : "source_management_template.csv");
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
    }catch(error){
        showAppAlert(error.message || "Unable to download template.");
    }
}

async function submitBulkImport(){
    const config = bulkImportConfig[currentBulkImportType];
    const input = document.getElementById("bulkImportFile");
    const result = document.getElementById("bulkImportResult");
    const file = input?.files?.[0];
    if(!file){
        showAppAlert("Please select a CSV file first.");
        return;
    }
    if(!file.name.toLowerCase().endsWith(".csv")){
        showAppAlert("Please upload a CSV file. The template is Excel-compatible and can be edited in Microsoft Excel.");
        return;
    }

    result.style.display = "block";
    result.style.background = "#eef5ff";
    result.style.color = "#244b78";
    result.textContent = "Importing...";

    try{
        const formData = new FormData();
        formData.append("file", file, file.name);

        const response = await fetch(API + config.importUrl, {
            method:"POST",
            headers:adminHeaders(),
            body:formData
        });
        const data = await response.json().catch(() => ({}));
        if(!response.ok){
            throw new Error(data.detail?.message || data.detail || "Bulk import failed.");
        }

        const errors = Array.isArray(data.errors) ? data.errors : [];
        result.style.background = errors.length ? "#fff8e8" : "#eefaf2";
        result.style.color = errors.length ? "#7a5200" : "#23613b";
        result.innerHTML = `<strong>Import completed.</strong><br>` +
            `Processed: ${Number(data.processed || 0)} &nbsp; | &nbsp; ` +
            `Created: ${Number(data.created || 0)} &nbsp; | &nbsp; ` +
            `Skipped duplicates: ${Number(data.skipped_duplicates || 0)} &nbsp; | &nbsp; ` +
            `Errors: ${errors.length}` +
            (errors.length ? `<div style="margin-top:8px;max-height:180px;overflow:auto">${errors.slice(0,50).map(e => `Row ${escapeHtml(e.row)}: ${escapeHtml(e.message)}`).join("<br>")}</div>` : "");

        if(currentBulkImportType === "sources"){
            await loadSources();
        }else{
            await loadFeeds();
            await loadSources();
        }
        await loadAdminDashboard();
    }catch(error){
        result.style.background = "#fff0f0";
        result.style.color = "#8b2c2c";
        result.textContent = error.message || "Bulk import failed.";
    }
}


/* =========================================================
   LOAD SOURCES
========================================================= */

let configuredSources = [];

function renderSources(rows){

    const tbody = document.getElementById("sourcesTable");

    if(!rows.length){
        tbody.innerHTML = `<tr><td colspan="8">No sources match your search.</td></tr>`;
        return;
    }

    tbody.innerHTML = rows.map(source => `
        <tr>
            <td>${source.id ?? ""}</td>
            <td>
                <strong>${escapeHtml(source.name || "")}</strong>
                <br>
                <small>${escapeHtml(source.source_code || "")}</small>
            </td>
            <td>${escapeHtml(source.coverage || "")}</td>
            <td>${escapeHtml(source.language || "")}</td>
            <td class="${source.active ? "status-active" : "status-inactive"}">
                ${source.active ? "ACTIVE" : "INACTIVE"}
            </td>
            <td>${escapeHtml(source.decision || "")}</td>
            <td>${source.article_count ?? 0}</td>
            <td>
                <button
                    class="table-action primary"
                    onclick="openSourceConfig(${Number(source.id)})"
                >
                    Configure
                </button>
            </td>
        </tr>
    `).join("");

    const counter = document.getElementById("sourceSearchCount");
    if(counter){
        counter.textContent = `${rows.length} of ${configuredSources.length} sources`;
    }
}

function filterSources(){

    const input = document.getElementById("sourceManagementSearch");
    const query = (input?.value || "").trim().toLowerCase();

    if(!query){
        renderSources(configuredSources);
        return;
    }

    const rows = configuredSources.filter(source => {
        const haystack = [
            source.id,
            source.name,
            source.source_code,
            source.website,
            source.rss_url,
            source.language,
            source.coverage,
            source.decision,
            source.active ? "active" : "inactive",
            source.article_count
        ].join(" ").toLowerCase();

        return haystack.includes(query);
    });

    renderSources(rows);
}

function clearSourceSearch(){
    const input = document.getElementById("sourceManagementSearch");
    if(input) input.value = "";
    filterSources();
    if(input) input.focus();
}

async function loadSources(){

    const tbody = document.getElementById("sourcesTable");

    tbody.innerHTML = `<tr><td colspan="8">Loading sources...</td></tr>`;

    try{

        const response = await fetch(
            API + "/api/admin/sources",
            { headers: adminHeaders() }
        );

        if(!response.ok){
            throw new Error("Unable to load sources.");
        }

        const data = await response.json();

        configuredSources = Array.isArray(data)
            ? data
            : (data.sources || data.items || []);

        renderSources(configuredSources);

    }catch(error){

        tbody.innerHTML = `<tr><td colspan="8">Unable to load sources. Check the Admin Key and API.</td></tr>`;

        const counter = document.getElementById("sourceSearchCount");
        if(counter) counter.textContent = "";
    }
}


function openSourceConfig(id = null){

    const source =
        id === null
            ? null
            : configuredSources.find(
                item => Number(item.id) === Number(id)
            );

    document.getElementById("sourceConfigId").value =
        source ? source.id : "";

    document.getElementById("sourceConfigTitle").textContent =
        source
            ? "Configure Media Source"
            : "Add Media Source";

    document.getElementById("sourceConfigName").value =
        source?.name || "";

    document.getElementById("sourceConfigCode").value =
        source?.source_code || "";

    document.getElementById("sourceConfigWebsite").value =
        source?.website || "";

    document.getElementById("sourceConfigCoverage").value =
        source?.coverage || "India";

    document.getElementById("sourceConfigLanguage").value =
        source?.language || "English";

    document.getElementById("sourceConfigActive").value =
        String(source?.active ?? false);

    document.getElementById("sourceConfigDecision").value =
        source?.decision || "HOLD";

    document.getElementById("sourceDeleteButton").style.display =
        source ? "inline-block" : "none";

    document.getElementById("sourceConfigModal")
        .classList.add("show");
}


function closeSourceConfig(){

    document.getElementById("sourceConfigModal")
        .classList.remove("show");
}


async function saveSourceConfig(){

    const id =
        document.getElementById("sourceConfigId")
            .value
            .trim();

    const payload = {

        source_code:
            document.getElementById("sourceConfigCode")
                .value
                .trim(),

        name:
            document.getElementById("sourceConfigName")
                .value
                .trim(),

        website:
            document.getElementById("sourceConfigWebsite")
                .value
                .trim(),

        coverage:
            document.getElementById("sourceConfigCoverage")
                .value
                .trim() || "India",

        language:
            document.getElementById("sourceConfigLanguage")
                .value
                .trim() || "English",

        active:
            document.getElementById("sourceConfigActive")
                .value === "true",

        decision:
            document.getElementById("sourceConfigDecision")
                .value
                .trim() || "HOLD"

    };

    if(
        !payload.name ||
        !payload.source_code ||
        !payload.website
    ){

        showAppAlert(
            "Source Name, Source Code and Website are required."
        );

        return;
    }

    try{

        const response =
            await fetch(

                API +
                (
                    id
                        ? "/api/admin/sources/" + id
                        : "/api/admin/sources"
                ),

                {

                    method:
                        id ? "PUT" : "POST",

                    headers:{

                        ...adminHeaders(),

                        "Content-Type":
                            "application/json"

                    },

                    body:
                        JSON.stringify(payload)

                }

            );


        const data =
            await response.json()
                .catch(() => ({}));


        if(!response.ok){

            throw new Error(
                data.detail?.message ||
                data.detail ||
                "Unable to save media source."
            );

        }


        closeSourceConfig();

        await loadSources();

        await loadAdminDashboard();

        showAppAlert(
            id
                ? "Media source updated successfully."
                : "Media source added successfully."
        );

    }

    catch(error){

        showAppAlert(
            error.message ||
            "Unable to save media source."
        );

    }
}


async function deleteSourceConfig(){

    const id =
        document.getElementById("sourceConfigId")
            .value
            .trim();

    if(!id){
        return;
    }

    if(!(await showAppConfirm(
        "Delete this media source? Sources with feeds or articles may not be deletable.",
        {title:"Delete Media Source", okText:"Delete", type:"warning", danger:true}
    ))){
        return;
    }

    try{

        const response =
            await fetch(

                API +
                "/api/admin/sources/" +
                id,

                {

                    method:"DELETE",

                    headers:
                        adminHeaders()

                }

            );


        const data =
            await response.json()
                .catch(() => ({}));


        if(!response.ok){

            throw new Error(
                data.detail?.message ||
                data.detail ||
                "Unable to delete media source."
            );

        }


        closeSourceConfig();

        await loadSources();

        await loadAdminDashboard();

        showAppAlert("Media source deleted successfully.");

    }

    catch(error){

        showAppAlert(
            error.message ||
            "Unable to delete media source."
        );

    }
}


/* =========================================================
   RSS FEED BULK SELECTION / ACTIONS
========================================================= */

let selectedFeedIds = new Set();

function toggleFeedSelection(id, checked){
    const feedId = Number(id);
    if(checked){
        selectedFeedIds.add(feedId);
    }else{
        selectedFeedIds.delete(feedId);
    }
    syncSelectAllFeeds();
}

function toggleAllVisibleFeeds(checked){
    const input = document.getElementById("feedManagementSearch");
    const query = (input?.value || "").trim().toLowerCase();
    const visibleRows = !query ? configuredFeeds : configuredFeeds.filter(feed => {
        const haystack = [
            feed.id, feed.feed_name, feed.feed_url, feed.source_name, feed.source,
            feed.category, feed.language, feed.decision,
            feed.rss_verified ? "verified yes" : "verified no",
            feed.active ? "active" : "inactive"
        ].join(" ").toLowerCase();
        return haystack.includes(query);
    });

    visibleRows.forEach(feed => {
        const id = Number(feed.id);
        if(checked) selectedFeedIds.add(id);
        else selectedFeedIds.delete(id);
    });

    renderFeeds(visibleRows);
}

function syncSelectAllFeeds(){
    const selectAll = document.getElementById("selectAllFeeds");
    if(!selectAll) return;

    const boxes = Array.from(document.querySelectorAll(".feed-select-checkbox"));
    if(!boxes.length){
        selectAll.checked = false;
        selectAll.indeterminate = false;
        return;
    }

    const checkedCount = boxes.filter(box => box.checked).length;
    selectAll.checked = checkedCount === boxes.length;
    selectAll.indeterminate = checkedCount > 0 && checkedCount < boxes.length;

    const count = document.getElementById("feedSelectionCount");
    if(count){
        const total = selectedFeedIds.size;
        count.textContent = total ? `${total} feed${total === 1 ? "" : "s"} selected` : "No feeds selected";
    }
}

function getSelectedFeedIds(){
    return Array.from(selectedFeedIds).filter(id => configuredFeeds.some(feed => Number(feed.id) === id));
}

function showRssBulkProgress(operation, total){
    const modal = document.getElementById("rssBulkProgressModal");
    if(!modal) return;

    document.getElementById("rssBulkProgressTitle").textContent =
        operation === "test" ? "Testing RSS Feeds" : "Ingesting RSS Feeds";
    document.getElementById("rssBulkProgressText").textContent =
        operation === "test"
            ? `Testing ${total} selected RSS feed${total === 1 ? "" : "s"}...`
            : `Ingesting ${total} selected RSS feed${total === 1 ? "" : "s"}...`;
    document.getElementById("rssBulkProgressBar").max = total || 1;
    document.getElementById("rssBulkProgressBar").value = 0;
    document.getElementById("rssBulkProgressPercent").textContent = "0%";
    document.getElementById("rssBulkProgressCurrent").textContent = "Preparing...";
    document.getElementById("rssBulkProgressDone").textContent = "0";
    document.getElementById("rssBulkProgressSuccess").textContent = "0";
    document.getElementById("rssBulkProgressFailed").textContent = "0";
    modal.classList.add("show");
}

function updateRssBulkProgress(operation, current, total, feed, successful, failed){
    const bar = document.getElementById("rssBulkProgressBar");
    const done = Math.min(current, total);
    const percent = total ? Math.round((done / total) * 100) : 100;
    if(bar){
        bar.max = total || 1;
        bar.value = done;
    }
    const percentEl = document.getElementById("rssBulkProgressPercent");
    if(percentEl) percentEl.textContent = `${percent}%`;
    const currentEl = document.getElementById("rssBulkProgressCurrent");
    if(currentEl){
        const feedName = feed?.feed_name || feed?.name || `Feed ${feed?.id ?? ""}`;
        currentEl.textContent = `${operation === "test" ? "Testing" : "Ingesting"}: ${feedName}`;
    }
    const doneEl = document.getElementById("rssBulkProgressDone");
    if(doneEl) doneEl.textContent = String(done);
    const successEl = document.getElementById("rssBulkProgressSuccess");
    if(successEl) successEl.textContent = String(successful);
    const failedEl = document.getElementById("rssBulkProgressFailed");
    if(failedEl) failedEl.textContent = String(failed);
}

function finishRssBulkProgress(operation, total, successful, failed){
    const bar = document.getElementById("rssBulkProgressBar");
    if(bar){
        bar.max = total || 1;
        bar.value = total || 1;
    }
    const percentEl = document.getElementById("rssBulkProgressPercent");
    if(percentEl) percentEl.textContent = "100%";
    const textEl = document.getElementById("rssBulkProgressText");
    if(textEl){
        textEl.textContent = operation === "test"
            ? `RSS Feed Test Completed — ${successful} successful, ${failed} failed.`
            : `RSS Feed Ingestion Completed — ${successful} successful, ${failed} failed.`;
    }
    const currentEl = document.getElementById("rssBulkProgressCurrent");
    if(currentEl) currentEl.textContent = "All selected feeds processed.";
    document.getElementById("rssBulkProgressDone").textContent = String(total);
    document.getElementById("rssBulkProgressSuccess").textContent = String(successful);
    document.getElementById("rssBulkProgressFailed").textContent = String(failed);
}

function closeRssBulkProgress(){
    document.getElementById("rssBulkProgressModal")?.classList.remove("show");
}

async function bulkTestSelectedFeeds(){
    const ids = getSelectedFeedIds();
    if(!ids.length){
        showAppAlert("Please select at least one RSS feed to test.");
        return;
    }

    if(!(await showAppConfirm(`Test ${ids.length} selected RSS feed${ids.length === 1 ? "" : "s"}?`, {title:"Confirm RSS Feed Test", okText:"Test Selected", type:"warning"}))) return;

    let successful = 0;
    let failed = 0;
    let totalEntries = 0;
    const failures = [];
    showRssBulkProgress("test", ids.length);

    for(let index = 0; index < ids.length; index++){
        const id = ids[index];
        const feed = configuredFeeds.find(item => Number(item.id) === Number(id)) || {id};
        updateRssBulkProgress("test", index, ids.length, feed, successful, failed);
        try{
            const response = await fetch(API + "/api/admin/feeds/" + id + "/test", {
                method:"POST",
                headers:adminHeaders()
            });
            const data = await response.json().catch(() => ({}));
            if(!response.ok){
                failed++;
                failures.push(`${id}: ${data.detail?.message || data.detail || "Test failed"}`);
            }else{
                successful++;
                totalEntries += Number(data.entries_found || 0);
            }
        }catch(error){
            failed++;
            failures.push(`${id}: Unable to reach test service`);
        }
        updateRssBulkProgress("test", index + 1, ids.length, feed, successful, failed);
    }

    finishRssBulkProgress("test", ids.length, successful, failed);
    await loadFeeds();
    await loadAdminDashboard();

    let message = `RSS Feed Test Completed\n\nSuccessful: ${successful}\nFailed: ${failed}\nEntries Found: ${totalEntries}`;
    if(failures.length){
        message += "\n\nFailed Feeds:\n" + failures.slice(0,10).join("\n");
        if(failures.length > 10) message += `\n...and ${failures.length - 10} more.`;
    }
    setTimeout(() => { closeRssBulkProgress(); showAppAlert(message); }, 350);
}

async function bulkIngestSelectedFeeds(){
    const ids = getSelectedFeedIds();
    if(!ids.length){
        showAppAlert("Please select at least one RSS feed to ingest.");
        return;
    }

    if(!(await showAppConfirm(`Ingest ${ids.length} selected RSS feed${ids.length === 1 ? "" : "s"}?`, {title:"Confirm RSS Feed Ingestion", okText:"Ingest Selected", type:"warning"}))) return;

    let successful = 0;
    let failed = 0;
    let added = 0;
    let skipped = 0;
    const failures = [];
    showRssBulkProgress("ingest", ids.length);

    for(let index = 0; index < ids.length; index++){
        const id = ids[index];
        const feed = configuredFeeds.find(item => Number(item.id) === Number(id)) || {id};
        updateRssBulkProgress("ingest", index, ids.length, feed, successful, failed);
        try{
            const response = await fetch(API + "/api/admin/feeds/" + id + "/ingest", {
                method:"POST",
                headers:adminHeaders()
            });
            const data = await response.json().catch(() => ({}));
            if(!response.ok){
                failed++;
                failures.push(`${id}: ${data.detail?.message || data.detail || "Ingestion failed"}`);
            }else{
                successful++;
                added += Number(data.added || 0);
                skipped += Number(data.skipped || 0);
            }
        }catch(error){
            failed++;
            failures.push(`${id}: Unable to reach ingestion service`);
        }
        updateRssBulkProgress("ingest", index + 1, ids.length, feed, successful, failed);
    }

    finishRssBulkProgress("ingest", ids.length, successful, failed);
    await loadFeeds();
    await loadAdminDashboard();

    let message = `RSS Feed Bulk Ingestion Completed\n\nSuccessful: ${successful}\nFailed: ${failed}\nArticles Added: ${added}\nArticles Skipped: ${skipped}`;
    if(failures.length){
        message += "\n\nFailed Feeds:\n" + failures.slice(0,10).join("\n");
        if(failures.length > 10) message += `\n...and ${failures.length - 10} more.`;
    }
    setTimeout(() => { closeRssBulkProgress(); showAppAlert(message); }, 350);
}


/* =========================================================
   LOAD FEEDS
========================================================= */

function renderFeeds(rows){

    const tbody = document.getElementById("feedsTable");

    if(!rows.length){
        tbody.innerHTML = `<tr><td colspan="8">No RSS feeds match your search.</td></tr>`;
        return;
    }

    tbody.innerHTML = rows.map(feed => `
        <tr>
            <td style="text-align:center">
                <input
                    type="checkbox"
                    class="feed-select-checkbox"
                    data-feed-id="${feed.id}"
                    aria-label="Select ${escapeHtml(feed.feed_name || "RSS feed")}"
                    ${selectedFeedIds.has(Number(feed.id)) ? "checked" : ""}
                    onchange="toggleFeedSelection(${Number(feed.id)}, this.checked)"
                    style="width:17px;height:17px;cursor:pointer"
                >
            </td>
            <td>${feed.id ?? ""}</td>
            <td>
                <strong>${escapeHtml(feed.feed_name || "")}</strong>
                <br>
                <small>${escapeHtml(feed.feed_url || "")}</small>
            </td>
            <td>${escapeHtml(feed.source_name || feed.source || "")}</td>
            <td>${escapeHtml(feed.category || "")}</td>
            <td>${feed.rss_verified ? "YES" : "NO"}</td>
            <td class="${feed.active ? "status-active" : "status-inactive"}">
                ${feed.active ? "ACTIVE" : "INACTIVE"}
            </td>
            <td>
                <div class="rss-feed-actions">
                    <button class="table-action" onclick="openFeedConfig(${feed.id})">Configure</button>
                    <button class="table-action" onclick="testFeed(${feed.id})">Test</button>
                    <button class="table-action" onclick="toggleFeed(${feed.id})">
                        ${feed.active ? "Deactivate" : "Activate"}
                    </button>
                    <button class="table-action primary" onclick="ingestFeed(${feed.id})">Ingest</button>
                    <button class="table-action" style="color:#a42525;border-color:#e2b9b9" onclick="deleteFeed(${feed.id})">Delete</button>
                </div>
            </td>
        </tr>
    `).join("");

    const counter = document.getElementById("feedSearchCount");
    if(counter){
        counter.textContent = `${rows.length} of ${configuredFeeds.length} feeds`;
    }

    syncSelectAllFeeds();
}

function filterFeeds(){

    const input = document.getElementById("feedManagementSearch");
    const query = (input?.value || "").trim().toLowerCase();

    if(!query){
        renderFeeds(configuredFeeds);
        return;
    }

    const rows = configuredFeeds.filter(feed => {
        const haystack = [
            feed.id,
            feed.feed_name,
            feed.feed_url,
            feed.source_name,
            feed.source,
            feed.category,
            feed.language,
            feed.decision,
            feed.rss_verified ? "verified yes" : "verified no",
            feed.active ? "active" : "inactive"
        ].join(" ").toLowerCase();

        return haystack.includes(query);
    });

    renderFeeds(rows);
}

function clearFeedSearch(){
    const input = document.getElementById("feedManagementSearch");
    if(input) input.value = "";
    filterFeeds();
    if(input) input.focus();
}

async function loadFeeds(){

    const tbody = document.getElementById("feedsTable");

    tbody.innerHTML = `<tr><td colspan="8">Loading feeds...</td></tr>`;

    try{

        const response = await fetch(
            API + "/api/admin/feeds",
            { headers: adminHeaders() }
        );

        if(!response.ok){
            throw new Error();
        }

        const data = await response.json();

        configuredFeeds = Array.isArray(data)
            ? data
            : (data.feeds || []);

        const validIds = new Set(configuredFeeds.map(feed => Number(feed.id)));
        selectedFeedIds = new Set(
            Array.from(selectedFeedIds).filter(id => validIds.has(id))
        );

        renderFeeds(configuredFeeds);

    }catch(error){

        tbody.innerHTML = `<tr><td colspan="8">Unable to load RSS feeds.</td></tr>`;

        const counter = document.getElementById("feedSearchCount");
        if(counter) counter.textContent = "";
    }
}


/* =========================================================
   RSS FEED CONFIGURATION
========================================================= */

let configuredFeeds = [];

async function openFeedConfig(id = null){

    const modal = document.getElementById("feedConfigModal");
    const select = document.getElementById("feedConfigSourceId");

    document.getElementById("feedConfigId").value = id ?? "";
    document.getElementById("feedConfigTitle").textContent = id ? "Configure RSS Feed" : "Add RSS Feed";
    document.getElementById("feedDeleteButton").style.display = id ? "inline-block" : "none";

    if(!configuredSources.length){
        try{ await loadSources(); }catch(_){ }
    }

    select.innerHTML = configuredSources.map(source =>
        `<option value="${Number(source.id)}">${escapeHtml(source.name || "Unnamed Source")}</option>`
    ).join("");

    if(id){
        try{
            const response = await fetch(API + "/api/admin/feeds/" + id,{headers:adminHeaders()});
            const data = await response.json().catch(() => ({}));
            if(!response.ok) throw new Error(data.detail?.message || data.detail || "Unable to load RSS feed.");

            document.getElementById("feedConfigSourceId").value = String(data.source_id ?? "");
            document.getElementById("feedConfigName").value = data.feed_name || "";
            document.getElementById("feedConfigUrl").value = data.feed_url || "";
            document.getElementById("feedConfigCategory").value = data.category || "National";
            document.getElementById("feedConfigLanguage").value = data.language || "English";
            document.getElementById("feedConfigActive").value = String(data.active ?? false);
            document.getElementById("feedConfigDecision").value = data.decision || "HOLD";
        }catch(error){
            showAppAlert(error.message || "Unable to load RSS feed.");
            return;
        }
    }else{
        document.getElementById("feedConfigName").value = "";
        document.getElementById("feedConfigUrl").value = "";
        document.getElementById("feedConfigCategory").value = "National";
        document.getElementById("feedConfigLanguage").value = "English";
        document.getElementById("feedConfigActive").value = "false";
        document.getElementById("feedConfigDecision").value = "HOLD";
    }

    modal.classList.add("show");
}

function closeFeedConfig(){
    document.getElementById("feedConfigModal").classList.remove("show");
}

async function saveFeedConfig(){

    const id = document.getElementById("feedConfigId").value.trim();
    const payload = {
        source_id: Number(document.getElementById("feedConfigSourceId").value),
        feed_name: document.getElementById("feedConfigName").value.trim(),
        feed_url: document.getElementById("feedConfigUrl").value.trim(),
        category: document.getElementById("feedConfigCategory").value.trim() || "National",
        language: document.getElementById("feedConfigLanguage").value.trim() || "English",
        active: document.getElementById("feedConfigActive").value === "true",
        decision: document.getElementById("feedConfigDecision").value.trim() || "HOLD"
    };

    if(!payload.source_id || !payload.feed_name || !payload.feed_url){
        showAppAlert("Media Source, Feed Name and RSS Feed URL are required.");
        return;
    }

    try{
        const response = await fetch(
            API + (id ? "/api/admin/feeds/" + id : "/api/admin/feeds"),
            {
                method: id ? "PUT" : "POST",
                headers:{...adminHeaders(),"Content-Type":"application/json"},
                body:JSON.stringify(payload)
            }
        );
        const data = await response.json().catch(() => ({}));
        if(!response.ok) throw new Error(data.detail?.message || data.detail || "Unable to save RSS feed.");

        closeFeedConfig();
        await loadFeeds();
        await loadSources();
        showAppAlert(id ? "RSS feed updated successfully." : "RSS feed added successfully.");
    }catch(error){
        showAppAlert(error.message || "Unable to save RSS feed.");
    }
}

async function toggleFeed(id){
    try{
        const response = await fetch(API + "/api/admin/feeds/" + id + "/toggle",{
            method:"POST",
            headers:adminHeaders()
        });
        const data = await response.json().catch(() => ({}));
        if(!response.ok) throw new Error(data.detail?.message || data.detail || "Unable to change feed status.");
        await loadFeeds();
    }catch(error){
        showAppAlert(error.message || "Unable to change feed status.");
    }
}

async function deleteFeed(id){
    if(!(await showAppConfirm("Delete this RSS feed? This will not delete already ingested articles.", {title:"Delete RSS Feed", okText:"Delete", type:"warning", danger:true}))) return;
    try{
        const response = await fetch(API + "/api/admin/feeds/" + id,{
            method:"DELETE",
            headers:adminHeaders()
        });
        const data = await response.json().catch(() => ({}));
        if(!response.ok) throw new Error(data.detail?.message || data.detail || "Unable to delete RSS feed.");
        closeFeedConfig();
        await loadFeeds();
        showAppAlert("RSS feed deleted successfully.");
    }catch(error){
        showAppAlert(error.message || "Unable to delete RSS feed.");
    }
}

/* =========================================================
   TEST FEED
========================================================= */

async function testFeed(id){

    try{

        const response =
            await fetch(

                API +
                "/api/admin/feeds/" +
                id +
                "/test",

                {

                    method:"POST",

                    headers:
                        adminHeaders()

                }

            );


        const data =
            await response.json();


        showAppAlert(

            response.ok

                ? (
                    "Feed Test Successful\n\n" +
                    "Entries Found: " +
                    (data.entries_found ?? 0)
                )

                : (
                    data.detail?.message ||
                    data.detail ||
                    "Feed test failed."
                )

        );

    }

    catch(error){

        showAppAlert(
            "Unable to test RSS feed."
        );

    }

}


/* =========================================================
   INGEST FEED
========================================================= */

async function ingestFeed(id){

    if(!(await showAppConfirm(
        "Do you want to ingest this RSS feed?",
        {title:"Confirm RSS Feed Ingestion", okText:"Ingest", type:"warning"}
    ))){

        return;

    }


    try{

        const response =
            await fetch(

                API +
                "/api/admin/feeds/" +
                id +
                "/ingest",

                {

                    method:"POST",

                    headers:
                        adminHeaders()

                }

            );


        const data =
            await response.json();


        if(!response.ok){

            throw new Error(
                data.detail?.message ||
                data.detail ||
                "Ingestion failed"
            );

        }


        showAppAlert(

            "Feed Ingestion Completed\n\n" +

            "Added: " +
            (data.added ?? 0) +

            "\nSkipped: " +
            (data.skipped ?? 0)

        );


        loadFeeds();

        loadAdminDashboard();

    }

    catch(error){

        showAppAlert(
            error.message ||
            "Feed ingestion failed."
        );

    }

}


/* =========================================================
   ePAPER AUTO-DISCOVERY
========================================================= */

let epaperDiscoveryResults = [];

function epaperEsc(value){
    return String(value ?? "")
        .replace(/&/g,"&amp;")
        .replace(/</g,"&lt;")
        .replace(/>/g,"&gt;")
        .replace(/"/g,"&quot;")
        .replace(/'/g,"&#39;");
}

async function loadEpaperPublishers(){
    const select = document.getElementById("epaperPublisher");
    if(!select) return;

    try{
        const response = await fetch(API + "/api/admin/epaper/publishers", {
            headers: adminHeaders()
        });
        const data = await response.json().catch(()=>({}));
        if(!response.ok) throw new Error(data.detail || "Unable to load ePaper publishers.");

        const publishers = Array.isArray(data.publishers) ? data.publishers : [];
        select.innerHTML = `<option value="">Custom publisher / enter URL</option>` +
            publishers.map(p => `<option value="${epaperEsc(p.id)}" data-epaper-url="${epaperEsc(p.url || "")}" data-epaper-language="${epaperEsc(p.language || "")}">${epaperEsc(p.name)} — ${epaperEsc(p.language || "")}</option>`).join("");

        onEpaperPublisherChange();
    }catch(error){
        select.innerHTML = `<option value="">Custom publisher / enter URL</option>`;
        const status = document.getElementById("epaperDiscoveryStatus");
        if(status) status.textContent = error.message || "Unable to load publisher list.";
    }
}

function onEpaperPublisherChange(){
    const select = document.getElementById("epaperPublisher");
    const urlInput = document.getElementById("epaperUrl");
    const langInput = document.getElementById("epaperLanguage");
    if(!select) return;

    const option = select.options[select.selectedIndex];
    const id = select.value;

    // IMPORTANT: every publisher selection must replace the previous URL.
    // Otherwise selecting Publisher B after Publisher A leaves A's URL in the field.
    if(id && option){
        const publisherUrl = option.getAttribute("data-epaper-url") || "";
        const publisherLanguage = option.getAttribute("data-epaper-language") || "";
        if(urlInput) urlInput.value = publisherUrl;
        if(langInput) langInput.value = publisherLanguage;
    }else{
        // Custom publisher: do not carry the previous publisher's URL/language.
        if(urlInput) urlInput.value = "";
        if(langInput) langInput.value = "";
    }
}

async function discoverEpaper(){
    const select = document.getElementById("epaperPublisher");
    const urlInput = document.getElementById("epaperUrl");
    const langInput = document.getElementById("epaperLanguage");
    const button = document.getElementById("epaperDiscoverButton");
    const status = document.getElementById("epaperDiscoveryStatus");

    const url = (urlInput?.value || "").trim();
    if(!url){
        showAppAlert("Please enter an ePaper / edition page URL.");
        return;
    }

    const publisherId = select?.value || null;
    const publisherText = select?.options?.[select.selectedIndex]?.textContent || "Custom Publisher";
    const publisher = publisherId ? publisherText.split(" — ")[0].trim() : "Custom Publisher";

    if(button){
        button.disabled = true;
        button.textContent = "Discovering…";
    }
    if(status) status.textContent = "Opening publisher page and discovering official edition links…";

    try{
        const response = await fetch(API + "/api/admin/epaper/discover", {
            method:"POST",
            headers:{...adminHeaders(), "Content-Type":"application/json"},
            body:JSON.stringify({
                publisher_id: publisherId,
                publisher,
                url,
                language:(langInput?.value || "").trim() || null
            })
        });
        const data = await response.json().catch(()=>({}));
        if(!response.ok) throw new Error(data.detail || "ePaper discovery failed.");

        epaperDiscoveryResults = Array.isArray(data.results) ? data.results : [];
        renderEpaperDiscoveryResults();
        if(status) status.textContent = epaperDiscoveryResults.length
            ? `Discovery complete — ${epaperDiscoveryResults.length} official edition link(s) found.`
            : "Discovery completed, but no edition links matched the ePaper patterns on this page.";
    }catch(error){
        epaperDiscoveryResults = [];
        renderEpaperDiscoveryResults();
        if(status) status.textContent = error.message || "ePaper discovery failed.";
    }finally{
        if(button){
            button.disabled = false;
            button.textContent = "🔎 Discover Editions";
        }
    }
}

function renderEpaperDiscoveryResults(){
    const body = document.getElementById("epaperDiscoveryTable");
    const count = document.getElementById("epaperDiscoveryCount");
    const selectAll = document.getElementById("epaperSelectAll");
    if(!body) return;

    if(count) count.textContent = String(epaperDiscoveryResults.length);
    if(selectAll) selectAll.checked = false;

    if(!epaperDiscoveryResults.length){
        body.innerHTML = `<tr><td colspan="7" style="color:#718095">No discovered editions.</td></tr>`;
        return;
    }

    body.innerHTML = epaperDiscoveryResults.map((row,index)=>`
        <tr>
            <td><input class="epaper-result-check" data-index="${index}" type="checkbox" style="width:17px;height:17px;cursor:pointer"></td>
            <td><strong>${epaperEsc(row.publisher)}</strong></td>
            <td>${epaperEsc(row.edition || "Edition")}</td>
            <td>${epaperEsc(row.date || "—")}</td>
            <td>${epaperEsc(row.language || "—")}</td>
            <td><span class="epaper-chip">✓ ${epaperEsc(row.status || "discovered")}</span></td>
            <td><a href="${epaperEsc(row.url)}" target="_blank" rel="noopener noreferrer">Open Official ↗</a></td>
        </tr>
    `).join("");
}

function toggleAllEpaperResults(checked){
    document.querySelectorAll(".epaper-result-check").forEach(cb=>{ cb.checked = checked; });
}

async function saveSelectedEpapers(){
    const selected = Array.from(document.querySelectorAll(".epaper-result-check:checked"))
        .map(cb => epaperDiscoveryResults[Number(cb.dataset.index)])
        .filter(Boolean);

    if(!selected.length){
        showAppAlert("Select at least one discovered edition first.");
        return;
    }

    let saved = 0;
    let failed = 0;
    for(const row of selected){
        try{
            const response = await fetch(API + "/api/admin/epaper/saved", {
                method:"POST",
                headers:{...adminHeaders(), "Content-Type":"application/json"},
                body:JSON.stringify({
                    publisher:row.publisher || "",
                    edition:row.edition || "Edition",
                    date:row.date || null,
                    language:row.language || null,
                    url:row.url,
                    source_url:row.source_url || null
                })
            });
            if(response.ok) saved++;
            else failed++;
        }catch(_){ failed++; }
    }

    await loadSavedEpapers();
    showAppAlert(`ePaper directory update completed.\n\nSaved / already present: ${saved}\nFailed: ${failed}`);
}

async function loadSavedEpapers(){
    const wrap = document.getElementById("epaperSavedList");
    if(!wrap) return;
    try{
        const response = await fetch(API + "/api/admin/epaper/saved", {headers:adminHeaders()});
        const data = await response.json().catch(()=>({}));
        if(!response.ok) throw new Error(data.detail || "Unable to load saved ePapers.");
        const rows = Array.isArray(data.results) ? data.results : [];
        if(!rows.length){
            wrap.innerHTML = `<div class="epaper-note">No saved editions yet. Discover editions and save the ones you want in the directory.</div>`;
            return;
        }
        wrap.innerHTML = rows.map(row=>`
            <div class="epaper-saved-card">
                <div class="epaper-saved-card-title">📰 ${epaperEsc(row.publisher)} — ${epaperEsc(row.edition)}</div>
                <div class="epaper-saved-card-meta">${epaperEsc(row.date || "Date not detected")} · ${epaperEsc(row.language || "Language not detected")}</div>
                <div style="display:flex;gap:7px;flex-wrap:wrap">
                    <a class="table-action primary" href="${epaperEsc(row.url)}" target="_blank" rel="noopener noreferrer">Open Official ePaper</a>
                    <button class="table-action" onclick="deleteSavedEpaper('${epaperEsc(row.id)}')">Remove</button>
                </div>
            </div>
        `).join("");
    }catch(error){
        wrap.innerHTML = `<div class="epaper-note">${epaperEsc(error.message || "Unable to load saved ePapers.")}</div>`;
    }
}

async function deleteSavedEpaper(id){
    if(!(await showAppConfirm("Remove this ePaper edition from the saved directory?", {title:"Remove Saved ePaper", okText:"Remove", type:"warning"}))) return;
    try{
        const response = await fetch(API + "/api/admin/epaper/saved/" + encodeURIComponent(id), {
            method:"DELETE",
            headers:adminHeaders()
        });
        const data = await response.json().catch(()=>({}));
        if(!response.ok) throw new Error(data.detail || "Unable to remove saved ePaper.");
        await loadSavedEpapers();
    }catch(error){
        showAppAlert(error.message || "Unable to remove saved ePaper.");
    }
}

/* =========================================================
   DASHBOARD STATS
========================================================= */

async function loadAdminDashboard(){

    try{

        const sourcesResponse =
            await fetch(

                API +
                "/api/admin/sources",

                {
                    headers:
                        adminHeaders()
                }

            );


        const feedsResponse =
            await fetch(

                API +
                "/api/admin/feeds",

                {
                    headers:
                        adminHeaders()
                }

            );


        const sourcesData =
            sourcesResponse.ok
                ? await sourcesResponse.json()
                : [];


        const feedsData =
            feedsResponse.ok
                ? await feedsResponse.json()
                : [];


        const sources =
            Array.isArray(sourcesData)
                ? sourcesData
                : (
                    sourcesData.sources ||
                    []
                );


        const feeds =
            Array.isArray(feedsData)
                ? feedsData
                : (
                    feedsData.feeds ||
                    []
                );


        const activeSources =
            sources.filter(
                item => item.active
            );


        const articleCount =
            sources.reduce(

                (total,item) =>

                    total +
                    Number(
                        item.article_count || 0
                    ),

                0

            );


        document
            .getElementById("statSources")
            .textContent =
                sources.length;


        document
            .getElementById("statActiveSources")
            .textContent =
                activeSources.length;


        document
            .getElementById("statFeeds")
            .textContent =
                feeds.length;


        document
            .getElementById("statArticles")
            .textContent =
                articleCount;

    }

    catch(error){

        console.log(
            "Dashboard statistics unavailable."
        );

    }

}


/* =========================================================
   AUTOMATIC GLOBAL RSS DISCOVERY
========================================================= */

async function loadAutoRssDiscoverySetting(){
    const toggle = document.getElementById("autoRssDiscoveryToggle");
    const interval = document.getElementById("autoRssDiscoveryInterval");
    const status = document.getElementById("autoRssDiscoveryStatus");
    const result = document.getElementById("autoRssDiscoveryResult");
    if(!toggle || !interval || !status || !result) return;

    try{
        const response = await fetch(API + "/api/admin/settings/auto-rss-discovery", {
            headers: adminHeaders()
        });
        const data = await response.json();
        if(!response.ok) throw new Error(data.detail || "Unable to load automatic RSS discovery setting.");

        toggle.checked = Boolean(data.enabled);
        interval.value = String(data.interval_minutes || 360);
        status.textContent = data.enabled ? "ON — automatic discovery is active" : "OFF";
        status.style.color = data.enabled ? "#17623a" : "#68778a";
        result.textContent = data.enabled
            ? `Automatic discovery is ON. The service will run every ${formatDiscoveryInterval(data.interval_minutes)}.`
            : "Automatic discovery is disabled.";
    }catch(error){
        status.textContent = "UNAVAILABLE";
        status.style.color = "#8b2c2c";
        result.textContent = "✕ " + (error.message || "Unable to load setting.");
    }
}

function formatDiscoveryInterval(minutes){
    if(Number(minutes) === 60) return "1 hour";
    if(Number(minutes) % 1440 === 0) return `${Number(minutes)/1440} day(s)`;
    if(Number(minutes) % 60 === 0) return `${Number(minutes)/60} hour(s)`;
    return `${minutes} minutes`;
}

async function toggleAutoRssDiscovery(){
    const toggle = document.getElementById("autoRssDiscoveryToggle");
    const interval = document.getElementById("autoRssDiscoveryInterval");
    const status = document.getElementById("autoRssDiscoveryStatus");
    const result = document.getElementById("autoRssDiscoveryResult");
    if(!toggle || !interval || !status || !result) return;

    const enabled = toggle.checked;
    const intervalMinutes = Number(interval.value || 360);
    result.textContent = enabled ? "Enabling automatic discovery..." : "Disabling automatic discovery...";

    try{
        const response = await fetch(API + "/api/admin/settings/auto-rss-discovery", {
            method: "PUT",
            headers: {...adminHeaders(), "Content-Type":"application/json"},
            body: JSON.stringify({enabled, interval_minutes: intervalMinutes})
        });
        const data = await response.json();
        if(!response.ok) throw new Error(data.detail || "Unable to update automatic discovery.");

        status.textContent = enabled ? "ON — automatic discovery is active" : "OFF";
        status.style.color = enabled ? "#17623a" : "#68778a";
        result.textContent = enabled
            ? `✓ Automatic global RSS discovery enabled. Next cycle will run automatically every ${formatDiscoveryInterval(intervalMinutes)}.`
            : "✓ Automatic global RSS discovery disabled.";
    }catch(error){
        toggle.checked = !enabled;
        result.textContent = "✕ " + (error.message || "Unable to update setting.");
    }
}

async function runAutoRssDiscoveryNow(){
    const result = document.getElementById("autoRssDiscoveryResult");
    const progress = document.getElementById("autoRssDiscoveryProgress");
    const bar = document.getElementById("autoRssDiscoveryProgressBar");
    const timer = document.getElementById("autoRssDiscoveryTimer");
    const progressText = document.getElementById("autoRssDiscoveryProgressText");
    if(!result) return;

    const confirmed = await openAppMessageModal(
        "The system will discover publisher RSS/Atom feeds from global news discovery sources, add new publishers/feeds to the database, and ingest their available articles. Continue?",
        {confirm:true, title:"Run Global RSS Discovery", okText:"Start Discovery"}
    );
    if(!confirmed) return;

    result.textContent = "Starting discovery…";
    if(progress) progress.style.display = "block";
    if(bar) bar.style.width = "5%";
    if(timer) timer.textContent = "00:00";
    if(progressText) progressText.textContent = "Starting global RSS discovery…";

    const started = Date.now();
    let elapsedTimer = null;
    let pollTimer = null;
    let finished = false;

    const updateTimer = () => {
        const elapsed = Math.floor((Date.now() - started) / 1000);
        const mm = String(Math.floor(elapsed / 60)).padStart(2,"0");
        const ss = String(elapsed % 60).padStart(2,"0");
        if(timer) timer.textContent = `${mm}:${ss}`;
    };
    elapsedTimer = setInterval(updateTimer, 1000);
    updateTimer();

    const finishUI = (failed=false) => {
        if(finished) return;
        finished = true;
        clearInterval(elapsedTimer);
        clearInterval(pollTimer);
        if(failed){
            if(bar) bar.style.width = "100%";
            if(progressText) progressText.textContent = "Discovery failed.";
        }
        setTimeout(() => { if(progress) progress.style.display = "none"; }, 2200);
    };

    const pollStatus = async () => {
        try{
            const response = await fetch(API + "/api/admin/settings/auto-rss-discovery/status", {
                method:"GET",
                headers:adminHeaders()
            });
            const data = await response.json();
            if(!response.ok) throw new Error(data.detail?.message || data.detail || "Unable to read discovery status.");

            const pct = Math.max(0, Math.min(100, Number(data.progress ?? 0)));
            if(bar) bar.style.width = `${pct}%`;

            const stageText = {
                starting: "Starting global RSS discovery…",
                discovering: "Discovering publishers and RSS/Atom feeds…",
                ingesting: "Ingesting new articles and removing duplicates…",
                completed: "Discovery and ingestion completed.",
                failed: "Discovery failed.",
                cancelled: "Discovery cancelled."
            };
            if(progressText) progressText.textContent = stageText[data.stage] || "Processing global RSS discovery…";

            if(data.running) return;

            if(data.stage === "completed" && data.result){
                const r = data.result;
                if(bar) bar.style.width = "100%";
                result.innerHTML = `<strong>✓ Discovery completed.</strong><br>` +
                    `Queries: ${Number(r.queries_run || 0)} &nbsp; | &nbsp; ` +
                    `New Sources: ${Number(r.new_sources || 0)} &nbsp; | &nbsp; ` +
                    `New Feeds: ${Number(r.new_feeds || 0)} &nbsp; | &nbsp; ` +
                    `Articles Added: ${Number(r.articles_ingested || r.articles_added || 0)}`;
                finishUI();
            } else if(data.stage === "failed"){
                result.textContent = "✕ " + (data.error || "Discovery failed.");
                finishUI(true);
            }
        }catch(error){
            // A temporary polling failure must not be treated as discovery failure.
            // Keep polling; the server-side job continues independently.
            if(progressText) progressText.textContent = "Discovery is still running…";
        }
    };

    try{
        const response = await fetch(API + "/api/admin/settings/auto-rss-discovery/run-now", {
            method:"POST",
            headers:adminHeaders()
        });
        const data = await response.json();
        if(!response.ok) throw new Error(data.detail?.message || data.detail || "Unable to start discovery.");

        if(data.status === "running"){
            result.textContent = "A discovery job is already running. Showing its progress…";
        } else {
            result.textContent = "Discovery is running…";
        }

        await pollStatus();
        pollTimer = setInterval(pollStatus, 1000);
    }catch(error){
        result.textContent = "✕ " + (error.message || "Unable to start discovery.");
        finishUI(true);
    }
}

/* =========================================================
   SEARCH LIMIT ADMIN SETTINGS
========================================================= */

async function loadSearchLimitSetting(){

    const input =
        document.getElementById("adminFreeSearchLimit");

    const result =
        document.getElementById("searchLimitResult");

    if(!input || !result){
        return;
    }

    result.textContent =
        "Loading current search limit...";

    try{

        const response =
            await fetch(
                API + "/api/admin/settings/search-quota",
                {
                    headers: adminHeaders()
                }
            );

        const data = await response.json();

        if(!response.ok){
            throw new Error(
                data.detail ||
                "Unable to load search limit."
            );
        }

        input.value =
            data.free_search_limit ?? "";

        result.textContent =
            "Current free search limit: " +
            data.free_search_limit +
            " searches per user.";

    }
    catch(error){

        result.textContent =
            "✕ Unable to load search limit: " +
            (error.message || "Unknown error");

    }

}


async function saveSearchLimitSetting(){

    const input =
        document.getElementById("adminFreeSearchLimit");

    const result =
        document.getElementById("searchLimitResult");

    if(!input || !result){
        return;
    }

    const value = Number(input.value);

    if(!Number.isInteger(value) || value < 0 || value > 1000000){
        result.textContent =
            "Please enter a whole number between 0 and 1,000,000.";
        return;
    }

    result.textContent =
        "Saving search limit...";

    try{

        const response =
            await fetch(
                API + "/api/admin/settings/search-quota",
                {
                    method: "PUT",
                    headers: {
                        ...adminHeaders(),
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({
                        free_search_limit: value
                    })
                }
            );

        const data = await response.json();

        if(!response.ok){
            throw new Error(
                data.detail ||
                "Unable to save search limit."
            );
        }

        result.textContent =
            "✓ Search limit updated to " +
            data.free_search_limit +
            " searches per user.";

        // Refresh the public quota card immediately so the new limit is
        // visible without reloading the page.
        await refreshQuota();

    }
    catch(error){

        result.textContent =
            "✕ Save failed: " +
            (error.message || "Unknown error");

    }

}


/* =========================================================
   RESET SEARCH QUOTA
========================================================= */

async function callResetEndpoint(targetUserKey){

    const headers =
        {

            ...adminHeaders(),

            "X-User-Key":
                targetUserKey

        };


    /*
        Tries the dedicated admin route first.
        Then falls back to the existing reset route.
    */


    let response =
        await fetch(

            API +
            "/api/admin/reset-search",

            {

                method:"POST",

                headers

            }

        );


    if(response.status === 404){

        response =
            await fetch(

                API +
                "/api/reset-search",

                {

                    method:"POST",

                    headers

                }

            );

    }


    let data = {};


    try{

        data =
            await response.json();

    }

    catch(error){}


    if(!response.ok){

        throw new Error(

            data.detail?.message ||

            data.detail ||

            "Reset endpoint failed."

        );

    }


    return data;

}



async function loadAdminSubscribers(){
    const body = document.getElementById("subscriberAdminTableBody");
    if(!body) return;

    try{
        const response = await fetch(API + "/api/admin/subscribers", {
            headers: adminHeaders()
        });
        const data = await response.json();

        if(!response.ok){
            throw new Error(data.detail || "Unable to load subscribers.");
        }

        if(!data.length){
            body.innerHTML =
                '<tr><td colspan="5" style="padding:10px;">No subscriber accounts found.</td></tr>';
            return;
        }

        body.innerHTML = data.map(account => `
            <tr>
                <td style="padding:10px;">${escapeHtml(account.email)}</td>
                <td style="padding:10px;">${account.subscribed ? "Active" : "Inactive"}</td>
                <td style="padding:10px;">${account.active ? "Enabled" : "Disabled"}</td>
                <td style="padding:10px;">${account.last_login_at ? new Date(account.last_login_at).toLocaleString() : "Never"}</td>
                <td style="padding:10px;">
                    <button class="btn ${account.subscribed && account.active ? "btn-danger" : "btn-primary"}"
                            onclick="toggleSubscriber(${account.id}, ${account.subscribed && account.active})">
                        ${account.subscribed && account.active ? "Revoke Access" : "Activate Access"}
                    </button>
                </td>
            </tr>
        `).join("");
    }catch(error){
        body.innerHTML =
            '<tr><td colspan="5" style="padding:10px;color:#c33d3d;">' +
            escapeHtml(error.message || "Unable to load subscribers.") +
            '</td></tr>';
    }
}

async function createPaidSubscriber(){
    const email = document.getElementById("adminSubscriberEmail").value.trim();
    const password = document.getElementById("adminSubscriberPassword").value;
    const result = document.getElementById("subscriberAdminResult");

    if(!email || !password){
        result.textContent = "Enter subscriber email and password.";
        return;
    }

    result.textContent = "Creating subscriber account...";

    try{
        const response = await fetch(API + "/api/admin/subscribers", {
            method:"POST",
            headers:{
                ...adminHeaders(),
                "Content-Type":"application/json"
            },
            body:JSON.stringify({
                email,
                password,
                subscribed:true,
                active:true
            })
        });

        const data = await response.json();

        if(!response.ok){
            throw new Error(data.detail || "Unable to create subscriber.");
        }

        result.textContent =
            "✓ Subscriber activated: " + data.email;
        document.getElementById("adminSubscriberEmail").value = "";
        document.getElementById("adminSubscriberPassword").value = "";
        await loadAdminSubscribers();

    }catch(error){
        result.textContent = "✕ " + (error.message || "Unable to create subscriber.");
    }
}

async function toggleSubscriber(id, currentlyActive){
    try{
        const response = await fetch(API + "/api/admin/subscribers/" + id, {
            method:"PUT",
            headers:{
                ...adminHeaders(),
                "Content-Type":"application/json"
            },
            body:JSON.stringify({
                subscribed: !currentlyActive,
                active: !currentlyActive
            })
        });

        const data = await response.json();

        if(!response.ok){
            throw new Error(data.detail || "Unable to update subscriber.");
        }

        await loadAdminSubscribers();
    }catch(error){
        showAppAlert(error.message || "Unable to update subscriber.");
    }
}


async function resetUserQuota(){

    const targetUserKey =
        document
            .getElementById("resetUserKey")
            .value
            .trim();


    const result =
        document
            .getElementById("resetResult");


    if(!targetUserKey){

        result.textContent =
            "Please enter a User Key.";

        return;

    }


    result.textContent =
        "Resetting search quota...";


    try{

        const data =
            await callResetEndpoint(
                targetUserKey
            );


        result.textContent =
            "✓ Search quota successfully reset for: " +
            targetUserKey;

    }

    catch(error){

        result.textContent =
            "✕ Reset failed: " +
            error.message;

    }

}


async function resetCurrentUserQuota(){

    document
        .getElementById("resetUserKey")
        .value =
            userKey;


    await resetUserQuota();

}


/* =========================================================
   QUOTA MODAL
========================================================= */

function openQuotaModal(){

    document
        .getElementById("quotaModal")
        .classList.add("show");

}


function closeQuotaModal(){

    document
        .getElementById("quotaModal")
        .classList.remove("show");

}


/* =========================================================
   SITE MESSAGE / CONFIRMATION MODAL
   Replaces browser-native showAppAlert()/confirm() dialogs so the site
   never shows "localhost:3000 says" messages.
========================================================= */
let appMessageResolver = null;

function closeAppMessageModal(result=false){
    const modal = document.getElementById("appMessageModal");
    if(!modal) return;
    modal.classList.remove("show", "is-error", "is-warning", "is-success");
    modal.setAttribute("aria-hidden", "true");

    const resolver = appMessageResolver;
    appMessageResolver = null;
    if(resolver){
        resolver(Boolean(result));
    }
}

function openAppMessageModal(message, options={}){
    const modal = document.getElementById("appMessageModal");
    if(!modal) return Promise.resolve(false);

    // Close any previous message safely before opening the next one.
    if(appMessageResolver){
        const oldResolver = appMessageResolver;
        appMessageResolver = null;
        oldResolver(false);
    }

    const isConfirm = Boolean(options.confirm);
    const type = options.type || "info";
    const title = options.title || (isConfirm ? "Please Confirm" : "Adamas Media Intelligence");
    const icon = options.icon || (type === "error" ? "!" : type === "warning" ? "⚠" : type === "success" ? "✓" : "ℹ");

    document.getElementById("appMessageTitle").textContent = title;
    document.getElementById("appMessageText").textContent = String(message ?? "");
    document.getElementById("appMessageIcon").textContent = icon;

    const cancel = document.getElementById("appMessageCancel");
    const ok = document.getElementById("appMessageOk");
    cancel.style.display = isConfirm ? "inline-flex" : "none";
    ok.textContent = options.okText || (isConfirm ? "Confirm" : "OK");
    ok.className = "btn " + (options.danger ? "btn-danger" : "btn-primary");

    modal.classList.remove("is-error", "is-warning", "is-success");
    if(["error","warning","success"].includes(type)) modal.classList.add("is-" + type);
    modal.classList.add("show");
    modal.setAttribute("aria-hidden", "false");

    setTimeout(() => ok.focus(), 0);

    return new Promise(resolve => {
        appMessageResolver = resolve;
    });
}

function showAppAlert(message, options={}){
    return openAppMessageModal(message, {
        ...options,
        confirm:false,
        okText:options.okText || "OK"
    });
}

function showAppConfirm(message, options={}){
    return openAppMessageModal(message, {
        ...options,
        confirm:true,
        okText:options.okText || "Confirm"
    });
}

// Clicking the dark backdrop closes an informational alert, but confirmation
// dialogs remain explicit so destructive actions cannot be accepted accidentally.
document.addEventListener("click", function(event){
    const modal = document.getElementById("appMessageModal");
    if(!modal || !modal.classList.contains("show")) return;
    if(event.target !== modal) return;
    const cancel = document.getElementById("appMessageCancel");
    if(cancel.style.display !== "none") closeAppMessageModal(false);
    else closeAppMessageModal(true);
});



/* =========================================================
   GENERAL
========================================================= */

function scrollToResults(){

    document
        .getElementById(
            "searchResultsSection"
        )
        .scrollIntoView(

            {
                behavior:"smooth",
                block:"start"
            }

        );

}


function setNavActive(element){

    document.querySelectorAll(".navigation .nav-link")
        .forEach(link => link.classList.remove("active"));

    if(element){
        element.classList.add("active");
    }

}


function showHome(element){

    closeInfoPage();

    setNavActive(
        element || document.querySelector(".navigation .nav-link")
    );

    window.scrollTo({

        top:0,

        behavior:"smooth"

    });

}


function openInfoPage(pageId){
    const overlay = document.getElementById("infoPageOverlay");
    if(!overlay) return;
    document.querySelectorAll(".info-page-view").forEach(page => page.style.display = "none");
    const page = document.getElementById(pageId);
    if(!page) return;
    page.style.display = "block";
    overlay.classList.add("show");
    overlay.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    overlay.scrollTop = 0;
}

function closeInfoPage(){
    const overlay = document.getElementById("infoPageOverlay");
    if(!overlay) return;
    overlay.classList.remove("show");
    overlay.setAttribute("aria-hidden", "true");
    document.querySelectorAll(".info-page-view").forEach(page => page.style.display = "none");
    document.body.style.overflow = "";
    const home = document.querySelector('.navigation .nav-link');
    setNavActive(home);
}

function openSourcesInfo(){
    openInfoPage("sourcesInfoPage");
}

function showAbout(){
    openInfoPage("aboutInfoPage");
}

function showContact(){
    openInfoPage("contactInfoPage");
}


function showSubscribe(){

    closeQuotaModal();

    showAppAlert(
        "Subscription and payment integration will be connected in the next phase."
    );

}


function cleanArticleText(value){

    if(value === null || value === undefined){
        return "";
    }

    let text = String(value);

    // Remove script/style blocks completely.
    text = text.replace(/<script[\s\S]*?<\/script>/gi, " ");
    text = text.replace(/<style[\s\S]*?<\/style>/gi, " ");

    // Remove media, links and other HTML tags from publisher summaries.
    text = text.replace(/<img[^>]*>/gi, " ");
    text = text.replace(/<a\b[^>]*>/gi, " ");
    text = text.replace(/<\/a>/gi, " ");
    text = text.replace(/<[^>]+>/g, " ");

    // Decode common HTML entities without introducing markup.
    const entityBox = document.createElement("textarea");
    entityBox.innerHTML = text;
    text = entityBox.value;

    // Remove any tags that may have been exposed after entity decoding.
    text = text.replace(/<[^>]+>/g, " ");

    // Normalize whitespace and publisher formatting noise.
    text = text
        .replace(/\u00a0/g, " ")
        .replace(/\s+/g, " ")
        .trim();

    if(!text){
        return "No summary available.";
    }

    // Keep the existing card compact.
    const maxLength = 420;

    if(text.length > maxLength){
        text = text.slice(0, maxLength).replace(/\s+\S*$/, "") + "…";
    }

    return text;
}


function escapeHtml(value){

    return String(value ?? "")

        .replace(
            /[&<>'"]/g,

            character =>

                ({

                    "&":"&amp;",
                    "<":"&lt;",
                    ">":"&gt;",
                    "'":"&#39;",
                    '"':"&quot;"

                })[character]

        );

}


function escapeAttribute(value){

    return escapeHtml(value);

}


/* =========================================================
   INITIAL LOAD
========================================================= */

updateSubscriberButton();
updatePublicAdminVisibility();
try{const v5q=new URLSearchParams(location.search).get('q');if(v5q&&document.getElementById('searchInput'))document.getElementById('searchInput').value=v5q;}catch(e){}
updateSubscriberWelcome(Boolean(subscriberToken));
refreshQuota();


/*
    If Admin login remains active in this browser tab,
    reopen the Admin Dashboard.
*/


if(adminKey){

    openAdminDashboard();

}


wireSearchFilters();
loadFilterOptions();


async function trackArticleTopic(articleId){
  const article=currentResults.find(x=>Number(x.id)===Number(articleId));
  if(!article)return;
  const name=(article.title||'Tracked Story').slice(0,80);
  try{await miFetch('/api/me/topics',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,query:article.title||''})});showAppAlert(`Subscriber topic “${name}” is now tracked.`,{type:'success',title:'Topic Tracking'});}catch(e){showAppAlert(e.message,{type:'error'});}
}
/* =========================================================
   ARTICLE ACTION BUTTONS
   Use event listeners instead of inline handlers so article titles
   containing quotes/apostrophes can never break the button.
========================================================= */
document.addEventListener('click', async (event) => {
  const trackBtn=event.target.closest('.article-track-topic');
  if(trackBtn){
    event.preventDefault();
    event.stopPropagation();
    await trackArticleTopic(Number(trackBtn.dataset.articleId));
    return;
  }
  const saveBtn=event.target.closest('.article-save-search');
  if(saveBtn){
    event.preventDefault();
    event.stopPropagation();
    if(!isPaidSubscriber()){
      showAppAlert('Save Search is a paid subscriber feature. Sign in with an active subscription to save this search for future use.',{type:'warning',title:'Subscriber Feature'});
      return;
    }
    const article=currentResults.find(x=>Number(x.id)===Number(saveBtn.dataset.articleId));
    if(!article){
      showAppAlert('This article is no longer available in the current result set.',{type:'warning',title:'Save Search'});
      return;
    }
    const title=String(article.title||'News Search').trim();
    openSaveSearchDialog(title, 'Saved: '+title.slice(0,60));
  }
});

/* =========================================================
   MEDIA INTELLIGENCE V3 UI
========================================================= */
let miTabsBound=false;
function bindMiTabs(){
  if(miTabsBound)return;
  const tabs=document.querySelectorAll('#miTabs .mi-tab');
  if(!tabs.length)return;
  miTabsBound=true;
  tabs.forEach(btn=>btn.addEventListener('click',()=>{tabs.forEach(x=>x.classList.remove('active'));btn.classList.add('active');const target=btn.dataset.tab;document.querySelectorAll('#intelligenceHub .mi-panel').forEach(p=>p.style.display=p.dataset.panel===target?'block':'none');if(target==='topics')loadTopics();if(target==='saved')loadSavedSearches();if(target==='alerts')loadAlerts();if(target==='stories')loadStoryClusters();}));
}
async function openIntelligenceHub(){
    const hub = document.getElementById('intelligenceHub');
    if(!hub) return;

    // Validate the current subscriber token before requesting protected
    // Intelligence data. This prevents a stale sessionStorage token from
    // appearing subscribed while the backend rejects the Intelligence call.
    if(subscriberToken){
        try{
            await refreshQuota();
        }catch(e){}
    }

    if(!isPaidSubscriber()){
        showAppAlert(
            "Subscriber Intelligence requires an active paid subscription. Please sign in again.",
            {
                type:"warning",
                title:"Subscriber Intelligence"
            }
        );
        return;
    }

    bindMiTabs();
    hub.classList.add('show');
    hub.setAttribute('aria-hidden','false');

    // Load only after the backend has confirmed the subscriber session.
    await loadIntelligenceDashboard();
}
function closeIntelligenceHub(){
  const hub=document.getElementById('intelligenceHub'); if(!hub)return;
  hub.classList.remove('show'); hub.setAttribute('aria-hidden','true');
}
function miEscape(v){return escapeHtml(String(v??''));}
function miBar(value,max){const pct=max?Math.min(100,Math.round(value/max*100)):0;return `<div class="mi-bar"><span style="width:${pct}%"></span></div>`;}
async function miFetch(path,options={}){
  const headers={...(options.headers||{}),'X-User-Key':userKey};
  if(subscriberToken) headers['X-Auth-Token']=subscriberToken;
  const r=await fetch(API+path,{...options,headers});
  if(!r.ok){let d={};try{d=await r.json()}catch(e){};throw new Error(d.detail?.message||d.detail||'Request failed');}
  return r.json();
}
function miRenderList(el,items,kind='count'){
  if(!el)return;
  if(!items?.length){el.innerHTML='<div class="mi-empty">No intelligence data available yet.</div>';return;}
  const max=Math.max(...items.map(x=>Number(x.count||x.mentions||x.article_count||1)),1);
  el.innerHTML=items.map(x=>`<div class="mi-item"><strong>${miEscape(x.name||x.topic||x.title||'')}</strong><div class="mi-small">${miEscape(x.count??x.mentions??x.article_count??'')}</div>${kind==='count'?miBar(Number(x.count||x.mentions||x.article_count||0),max):''}</div>`).join('');
}
async function loadIntelligenceDashboard(){
  if(!isPaidSubscriber()){
    return;
  }

  try{
    const d=await miFetch('/api/intelligence/dashboard');
    document.getElementById('miMetrics').innerHTML=[
      ['Articles / 7 days',d.metrics.articles_7d,'Indexed coverage'],['Sources / 7 days',d.metrics.sources_7d,'Active media sources'],['Story clusters',d.metrics.story_clusters,'Related coverage grouped'],['Tracked topics',d.metrics.tracked_topics,'Your monitored topics']
    ].map(x=>`<div class="mi-card"><div class="mi-muted">${x[0]}</div><div class="mi-value">${x[1]}</div><div class="mi-small">${x[2]}</div></div>`).join('');
    miRenderList(document.getElementById('miTrending'),d.trending);
    document.getElementById('miTopClusters').innerHTML=(d.clusters||[]).slice(0,8).map(c=>`<div class="mi-item mi-cluster"><div><strong>${miEscape(c.title)}</strong><div class="mi-small">${c.article_count} related articles · ${miEscape(c.keywords||'')}</div></div><button class="mi-btn" onclick="openCluster(${c.id})">View</button></div>`).join('')||'<div class="mi-empty">No story clusters yet.</div>';
    miRenderList(document.getElementById('miCategories'),d.categories);
    miRenderList(document.getElementById('miSources'),d.sources);
    miRenderList(document.getElementById('miLanguages'),d.languages);
    document.getElementById('miSourceIntel').innerHTML=(d.sources||[]).map(x=>`<div class="mi-item"><strong>${miEscape(x.name)}</strong><div class="mi-small">${x.count} articles in the last 7 days</div>${miBar(x.count,Math.max(...d.sources.map(z=>z.count),1))}</div>`).join('')||'<div class="mi-empty">No source activity yet.</div>';
    loadStoryClusters();
  }catch(e){
    const message = String(e && e.message || "");
    if(message.toLowerCase().includes("paid subscriber")){
      subscriberSessionValid = false;
      updateSubscriberWelcome(false);
      updateSubscriberButton();
      updateSaveSearchAccess();
      showAppAlert(
        "Your subscriber session is no longer valid. Please sign in again to open Media Intelligence.",
        {type:"warning",title:"Subscriber Session"}
      );
      return;
    }
    showAppAlert('Unable to load Media Intelligence: '+message,{type:'error'});
  }
}
async function loadStoryClusters(){
  try{const d=await miFetch('/api/intelligence/clusters?limit=40');document.getElementById('miStories').innerHTML=(d||[]).map(c=>`<div class="mi-item mi-cluster"><div><strong>${miEscape(c.title)}</strong><div class="mi-small">${c.article_count} articles · ${miEscape(c.keywords||'')}</div><div style="margin-top:5px;color:#536b81;font-size:12px">${miEscape(c.summary||'')}</div></div><button class="mi-btn" onclick="openCluster(${c.id})">Open Story</button></div>`).join('')||'<div class="mi-empty">No clusters available. Run ingestion first.</div>';}catch(e){showAppAlert(e.message,{type:'error'});}
}
async function openCluster(id){
  try{
    const d=await miFetch('/api/intelligence/clusters/'+id);
    const modal=document.getElementById('clusterDetailModal');
    if(!modal)return;
    document.getElementById('clusterDetailTitle').textContent=d.title||'Story Cluster';
    document.getElementById('clusterDetailSummary').textContent=(d.summary||'')+'  ·  '+(d.article_count||0)+' related articles';
    const list=document.getElementById('clusterDetailArticles');
    list.innerHTML=(d.articles||[]).map(a=>`<div class="mi-item"><strong>${miEscape(a.title)}</strong><div class="mi-small">${miEscape(a.source||'')} · ${miEscape(a.language||'')} · ${miEscape(a.sentiment||'Neutral')}</div><p style="font-size:12px;color:#526b82">${miEscape(a.summary||'')}</p>${a.url?`<a href="${escapeAttribute(a.url)}" target="_blank" rel="noopener" class="mi-btn" style="text-decoration:none;display:inline-block">Original ↗</a>`:''}</div>`).join('')||'<div class="mi-empty">No articles are currently linked to this story cluster.</div>';
    modal.classList.add('show'); modal.setAttribute('aria-hidden','false');
  }catch(e){showAppAlert(e.message,{type:'error'});}
}
function closeClusterDetail(){const modal=document.getElementById('clusterDetailModal');if(!modal)return;modal.classList.remove('show');modal.setAttribute('aria-hidden','true');}
async function createTrackedTopic(){const n=document.getElementById('miTopicName').value.trim(),q=document.getElementById('miTopicQuery').value.trim();if(!n||!q)return showAppAlert('Enter both a topic name and search query.',{type:'warning'});try{await miFetch('/api/me/topics',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,query:q})});document.getElementById('miTopicName').value='';document.getElementById('miTopicQuery').value='';loadTopics();}catch(e){showAppAlert(e.message,{type:'error'});}}
async function loadTopics(){try{const d=await miFetch('/api/me/topics');document.getElementById('miTopics').innerHTML=d.map(x=>`<div class="mi-item mi-cluster"><div><strong>${miEscape(x.name)}</strong><div class="mi-small">${miEscape(x.query)}</div></div><button class="mi-btn" onclick="deleteTopic(${x.id})">Remove</button></div>`).join('')||'<div class="mi-empty">No tracked topics.</div>';}catch(e){showAppAlert(e.message,{type:'error'});}}
async function deleteTopic(id){if(!await openAppMessageModal('Remove this tracked topic?',{confirm:true,title:'Remove Topic',danger:true,okText:'Remove'}))return;try{await miFetch('/api/me/topics/'+id,{method:'DELETE'});loadTopics();}catch(e){showAppAlert(e.message,{type:'error'});}}
function openSaveSearchDialog(queryOverride="", nameOverride=""){
  if(!isPaidSubscriber()) return showAppAlert("Save Search is available only to paid subscribers. Sign in with an active subscription to save this search for present and future use.",{type:"warning",title:"Subscriber Feature"});
  const query=String(queryOverride||lastSearchQuery||"").trim();
  if(!query) return showAppAlert("Run a search first, then save it.",{type:"warning",title:"Save Search"});
  const modal=document.getElementById("saveSearchModal");
  const input=document.getElementById("saveSearchName");
  const preview=document.getElementById("saveSearchPreview");
  if(!modal||!input)return;
  input.value=String(nameOverride||query).trim();
  preview.textContent=`Query: ${query}`;
  modal.dataset.queryOverride=query;
  modal.classList.add("show");
  modal.setAttribute("aria-hidden","false");
  setTimeout(()=>{input.focus();input.select();},0);
}
function closeSaveSearchDialog(result=false){
  const modal=document.getElementById("saveSearchModal");
  if(!modal)return;
  modal.classList.remove("show");
  modal.setAttribute("aria-hidden","true");
  if(result){ const input=document.getElementById("saveSearchName"); if(input) input.value=""; }
  delete modal.dataset.queryOverride;
}
async function confirmSaveCurrentSearch(){
  const name=document.getElementById("saveSearchName").value.trim();
  const modal=document.getElementById("saveSearchModal");
  const query=String(modal?.dataset?.queryOverride||lastSearchQuery||"").trim();
  if(!name||!query){return showAppAlert("Enter a name for this search.",{type:"warning",title:"Save Search"});}
  const button=document.getElementById("saveSearchConfirmButton");
  button.disabled=true;
  try{
    await miFetch('/api/me/saved-searches',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,query,filters:Object.fromEntries(getSearchFilterParams().entries())})});
    closeSaveSearchDialog(true);
    await loadSavedSearches(true);
    updateSaveSearchAccess();
    showAppAlert(`“${name}” has been saved to My Intelligence. This search is saved and will remain available for future runs.`,{type:'success',title:'Saved Search'});
    if(document.getElementById('miSaved')) loadSavedSearches();
  }catch(e){showAppAlert(e.message,{type:'error',title:'Save Search'});}
  finally{button.disabled=false;}
}

async function saveCurrentSearch(){
  if(!isPaidSubscriber()) return showAppAlert('Save Search is available only to paid subscribers. Sign in with an active subscription to save searches for future use.',{type:'warning',title:'Subscriber Feature'});
  const n=document.getElementById('miSavedName').value.trim();
  const q=document.getElementById('miSavedQuery').value.trim()||lastSearchQuery;
  if(!n||!q)return showAppAlert('Enter a name and query.',{type:'warning'});
  try{
    await miFetch('/api/me/saved-searches',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,query:q,filters:Object.fromEntries(getSearchFilterParams().entries())})});
    document.getElementById('miSavedName').value='';document.getElementById('miSavedQuery').value='';
    await loadSavedSearches(true); updateSaveSearchAccess();
    showAppAlert(`“${n}” is saved in My Intelligence. The query and selected filters are stored so you can run the same search again in the future.`,{type:'success',title:'Search Saved'});
  }catch(e){showAppAlert(e.message,{type:'error'});}
}
async function loadSavedSearches(silent=false){
  if(!isPaidSubscriber()){
    savedSearchesCache=[]; updateSaveSearchAccess();
    const el=document.getElementById('miSaved');
    if(el)el.innerHTML='<div class="mi-empty"><strong>🔒 Saved Searches — Paid Subscribers Only</strong><br><span class="mi-small">Sign in with an active subscription to save, run and manage searches for future use.</span></div>';
    return;
  }
  try{
    const d=await miFetch('/api/me/saved-searches'); savedSearchesCache=Array.isArray(d)?d:[]; updateSaveSearchAccess();
    const el=document.getElementById('miSaved'); if(!el)return;
    el.innerHTML=savedSearchesCache.map(x=>`<div class="mi-item mi-cluster"><div><strong>${miEscape(x.name)}</strong><div class="mi-small">${miEscape(x.query)}</div><div class="mi-small">✓ Saved search — available for future runs</div></div><div class="mi-actions"><button type="button" class="mi-btn primary" onclick="runSavedSearch(${x.id})">▶ Run</button><button type="button" class="mi-btn" onclick="deleteSaved(${x.id})">Remove</button></div></div>`).join('')||'<div class="mi-empty">No saved searches yet. Save one from a search result to keep it available for future runs.</div>';
  }catch(e){
    savedSearchesCache=[]; updateSaveSearchAccess();
    const el=document.getElementById('miSaved'); if(el)el.innerHTML='<div class="mi-empty">Unable to load Saved Searches. Please sign in again if your subscriber session has expired.</div>';
    if(!silent)showAppAlert(e.message,{type:'error',title:'Saved Searches'});
  }
}
function applySavedSearchFilters(filters){
  const f=filters||{};
  const date=document.getElementById('dateRange');if(date)date.value=String(f.date_range||'30');
  const lang=document.getElementById('languageFilter');if(lang)lang.value=String(f.language||'');
  const source=document.getElementById('sourceFilter');if(source)source.value=String(f.source||'');
  const wanted=String(f.categories||'').split(',').map(x=>x.trim()).filter(Boolean);
  const boxes=Array.from(document.querySelectorAll('.categoryFilter'));
  if(boxes.length){boxes.forEach((box,i)=>box.checked=i===0?wanted.length===0:wanted.some(v=>normaliseFilterText(v)===normaliseFilterText(box.value)));}
}
async function runSavedSearch(id){
  if(!isPaidSubscriber())return showAppAlert('Saved Search is available only to paid subscribers.',{type:'warning',title:'Subscriber Feature'});
  const item=savedSearchesCache.find(x=>Number(x.id)===Number(id));
  if(!item)return showAppAlert('Saved search not found. Refresh Saved Searches and try again.',{type:'warning',title:'Saved Search'});
  applySavedSearchFilters(item.filters||{}); closeIntelligenceHub();
  const q=String(item.query||'').trim(); document.getElementById('searchInput').value=q;
  await performSearch(q);
  showAppAlert(`Loaded “${item.name}”. The saved query and filters were applied and the search was run.`,{type:'success',title:'Saved Search Loaded'});
}
async function deleteSaved(id){
  if(!isPaidSubscriber())return showAppAlert('Saved Search is available only to paid subscribers.',{type:'warning',title:'Subscriber Feature'});
  if(!await openAppMessageModal('Remove this saved search? It will no longer be available for future runs.',{confirm:true,title:'Remove Saved Search',danger:true,okText:'Remove'}))return;
  try{await miFetch('/api/me/saved-searches/'+id,{method:'DELETE'});await loadSavedSearches(true);updateSaveSearchAccess();}catch(e){showAppAlert(e.message,{type:'error'});}
}

async function createNewsAlert(){const n=document.getElementById('miAlertName').value.trim(),q=document.getElementById('miAlertQuery').value.trim();if(!n||!q)return showAppAlert('Enter an alert name and query.',{type:'warning'});try{await miFetch('/api/me/alerts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,query:q,active:true})});document.getElementById('miAlertName').value='';document.getElementById('miAlertQuery').value='';loadAlerts();}catch(e){showAppAlert(e.message,{type:'error'});}}
async function loadAlerts(){try{const d=await miFetch('/api/me/alerts');document.getElementById('miAlerts').innerHTML=d.map(x=>`<div class="mi-item mi-cluster"><div><strong>${miEscape(x.name)}</strong><div class="mi-small">${miEscape(x.query)} · ${x.active?'Active':'Paused'}</div></div><button class="mi-btn" onclick="deleteAlert(${x.id})">Remove</button></div>`).join('')||'<div class="mi-empty">No alerts configured.</div>';}catch(e){showAppAlert(e.message,{type:'error'});}}
async function deleteAlert(id){if(!await openAppMessageModal('Remove this alert?',{confirm:true,title:'Remove Alert',danger:true,okText:'Remove'}))return;try{await miFetch('/api/me/alerts/'+id,{method:'DELETE'});loadAlerts();}catch(e){showAppAlert(e.message,{type:'error'});}}
async function generateBriefing(){const box=document.getElementById('miBriefing');box.textContent='Generating briefing…';try{const d=await miFetch('/api/me/briefing',{method:'POST'});box.textContent=d.body+`\n\n${d.article_count} articles considered · Generated ${new Date(d.generated_at).toLocaleString()}`;}catch(e){box.textContent='Unable to generate briefing: '+e.message;}}




/* Wake the API early (Render instances sleep when idle) and open the HTTPS
   connection before the user's first keystroke. */
fetch(API + "/health", {cache: "no-store"}).catch(() => {});


// Download the prediction index once the page has loaded.
if(document.readyState === "complete") loadPredictionIndex();
else window.addEventListener("load", loadPredictionIndex, {once: true});
