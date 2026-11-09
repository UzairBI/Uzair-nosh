// UI regression suite for the FINALIZED Nosh Assistant frontend. It only observes and interacts - it never edits the frontend.
//   node qa/ui_regression.js [--out qa/baseline/ui_results_before.json] [--shots qa/shots/before]
const fs = require('fs');
const { launch, sleep } = require('./cdp');
const BASE = process.env.NOSH_URL || 'http://127.0.0.1:8000';
const args = process.argv.slice(2);
const opt = k => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : null; };
const outFile = opt('--out'), shotDir = opt('--shots');
if (shotDir) fs.mkdirSync(shotDir, { recursive: true });

const results = [];
function rec(id, name, ok, detail = '') { results.push({ id, name, ok: !!ok, detail: String(detail).slice(0, 220) }); console.log((ok ? 'PASS ' : 'FAIL ') + id + ' ' + name + (ok ? '' : '   <- ' + String(detail).slice(0, 200))); }

const WAIT_IDLE = "!document.querySelector('.send').disabled && !document.querySelector('.typing')";
const lastBot = "(()=>{const r=[...document.querySelectorAll('#log .row:not(.me)')];return r[r.length-1]})()";

(async () => {
  const b = await launch({ width: 1366, height: 768 });
  const shot = async n => { if (shotDir) await b.shot(`${shotDir}/${n}.png`); };
  const ask = async (q, ms = 60000) => { await b.ev(`(q=>{const i=document.querySelector('#q');i.value=q;document.querySelector('#f').requestSubmit()})(${JSON.stringify(q)})`); await sleep(400); return b.waitFor(WAIT_IDLE, ms); };
  const resetDocs = async () => { await b.ev(`fetch('/api/docs').then(r=>r.json()).then(d=>Promise.all(d.docs.map(x=>fetch('/api/docs/delete',{method:'POST',body:JSON.stringify({id:x.id})}))))`); };
  try {
    // ---------------------------------------------------------------- R01-R06 static structure
    await b.goto(BASE + '/');
    await resetDocs(); await b.goto(BASE + '/');
    rec('R01', 'page loads: title, favicon links, no console errors/failed requests',
      await b.ev(`document.title==='Nosh Assistant' && !!document.querySelector('link[rel=icon]') && !!document.querySelector('link[rel="shortcut icon"]')`) && b.errors.length === 0 && b.failedReqs.length === 0,
      JSON.stringify({ errors: b.errors, failed: b.failedReqs }));
    const side = await b.ev(`JSON.stringify({
      logo:(()=>{const i=document.querySelector('aside .logo img');return !!i&&i.complete&&i.naturalWidth>0})(),
      newchat:document.querySelector('#new').innerText.trim(),
      mynosh:(()=>{const a=document.querySelector('.nav a');return a?[a.innerText.trim(),a.href,a.target]:null})(),
      help:document.querySelector('#help').innerText.trim(),
      hist:document.querySelector('#histBtn').innerText.trim(),
      user:document.querySelector('.user span').innerText.trim(),
      avatar:(()=>{const i=document.querySelector('.user img');return !!i&&i.complete&&i.naturalWidth>0})(),
      userArrow:!!document.querySelector('.user svg'),
      width:Math.round(document.querySelector('aside').getBoundingClientRect().width)})`);
    const S = JSON.parse(side);
    rec('R02', 'sidebar: logo, New chat, My Nosh (external link), Help & Support, History, name + photo, no arrow beside name',
      S.logo && S.newchat === 'New chat' && S.mynosh && S.mynosh[0] === 'My Nosh' && S.mynosh[1].includes('letsnosh.io') && S.mynosh[2] === '_blank' &&
      S.help === 'Help & Support' && S.hist.startsWith('History') && S.user === 'Uzair Meran Khan' && S.avatar && !S.userArrow, side);
    rec('R03', 'header: "Nosh Assistant", Online, subtitle', await b.ev(`(()=>{const h=document.querySelector('#chatView>header');return h.innerText.includes('Nosh Assistant')&&h.innerText.includes('Online')&&h.innerText.includes('Here to make everyday cooking effortless')})()`));
    const wel = await b.ev(`JSON.stringify({text:document.querySelector('#log .row .bubble').innerText, chips:[...document.querySelectorAll('#log .chips .chip')].map(c=>c.innerText.trim())})`);
    const W = JSON.parse(wel);
    rec('R04', 'welcome message + 3 starter chips (owner removed "Ask about a document")', W.text.includes("Hello! I'm Nosh.") &&
      JSON.stringify(W.chips) === JSON.stringify(['Find a quick recipe', 'Match my ingredients', 'Swap an ingredient']), wel);
    const comp = await b.ev(`JSON.stringify({ph:document.querySelector('#q').placeholder,attach:!!document.querySelector('#attach'),send:!!document.querySelector('.send'),
      accept:document.querySelector('#file').accept, multiple:document.querySelector('#file').multiple, cnt:document.querySelector('#attach .cnt').hidden})`);
    const C = JSON.parse(comp);
    rec('R05', 'composer: placeholder, paperclip, send, multi-file accept list, count badge hidden', C.ph === 'Ask Nosh anything about recipes, cooking, or your documents...' && C.attach && C.send &&
      C.multiple && ['.pdf', '.docx', '.csv', '.xlsx', '.json', '.txt'].every(e => C.accept.includes(e)) && C.cnt, comp);
    const lay = await b.ev(`JSON.stringify({promo:!!document.querySelector('.promo')||document.body.innerText.includes('made personal'),
      pageScroll:document.documentElement.scrollHeight>innerHeight+1, hScroll:document.documentElement.scrollWidth>innerWidth+1,
      formBottom:Math.round(document.querySelector('#f').getBoundingClientRect().bottom), ih:innerHeight,
      logOverflow:getComputedStyle(document.querySelector('#log')).overflowY, headerW:Math.round(document.querySelector('#chatView>header').getBoundingClientRect().width)})`);
    const L = JSON.parse(lay);
    rec('R06', 'layout: no right panel, page does not scroll, chat scrolls internally, composer pinned inside viewport, 820px column', !L.promo && !L.pageScroll && !L.hScroll && L.formBottom <= L.ih && L.logOverflow === 'auto' && L.headerW <= 821, lay);
    await shot('01_welcome');

    // ---------------------------------------------------------------- R07 chat basics, markdown, XSS
    rec('R07a', 'markdown renderer: table, code block, nested list, citations, [id] hidden', await b.ev(`(()=>{const h=md('| a | b |\\n|---|---|\\n| 1 | 2 |\\n\\n1. one\\n- sub\\n2. two\\n\\n\\u0060\\u0060\\u0060py\\nprint(1)\\n\\u0060\\u0060\\u0060\\nSee [1] and [id: ar:xyz]');
      return h.includes('<table>')&&h.includes('<pre><code>')&&h.includes('<ol start="1">')&&h.includes('<ul><li>sub')&&h.includes('class="cite"')&&!h.includes('ar:xyz')})()`));
    await ask('<img src=x onerror="window.__xss=1"> what is 2+2?');
    rec('R07b', 'XSS: HTML typed by the user is shown as text, not executed', await b.ev(`!window.__xss && [...document.querySelectorAll('#log .row.me .bubble')].pop().innerText.includes('<img src=x')`));
    await b.ev(`document.querySelector('#new').click()`); await sleep(800);

    // ---------------------------------------------------------------- R08 statistics through the real UI
    const okStats = await ask('How many recipes contain both garlic and onion?');
    const stat = await b.ev(`(()=>{const r=${lastBot};return r.querySelector('.bubble').innerText})()`);
    rec('R08', 'chat: statistics question answered with a number, send re-enabled, no typing dots', okStats && /\d{2},\d{3}/.test(stat) && /garlic/i.test(stat), stat.slice(0, 160));
    await shot('02_stats');
    await b.ev(`document.querySelector('#new').click()`); await sleep(600);

    // ---------------------------------------------------------------- R09-R12 recipe cards flow
    await ask('Find a penne pasta recipe.');
    const flow = await b.ev(`JSON.stringify((()=>{const r=${lastBot};const st=r.querySelector('.stack');const kids=[...st.children].map(c=>c.className||c.tagName);
      const cards=[...r.querySelectorAll('.card')];return {order:kids, n:cards.length, imgs:cards.every(c=>{const i=c.querySelector('img');return i.complete&&i.naturalWidth>0}),
      titles:cards.map(c=>c.querySelector('.t').innerText), meta:cards.map(c=>c.querySelector('.m').innerText.replace(/\\n/g,' ')),
      chips:[...document.querySelectorAll('#log .chips .chip')].map(c=>c.innerText.trim()), body:document.querySelector('#log').innerText}})())`);
    const F = JSON.parse(flow);
    rec('R09', 'recipe search: text first, then cards (image, title, steps/ingredients), then follow-up chips', F.n >= 3 && F.order[0].includes('bubble') && F.order.indexOf('cards') > F.order.indexOf('bubble md') && F.imgs &&
      JSON.stringify(F.chips) === JSON.stringify(['Show me more', 'Make it dairy-free', 'Swap an ingredient']), JSON.stringify({ order: F.order, n: F.n, chips: F.chips }));
    rec('R10', 'no recipe ids and no "full details" control anywhere', !/\[id:|ar:[A-Za-z0-9]{8}|fn:[A-Za-z0-9]{8}|epi:[A-Za-z0-9]{8}/.test(F.body) && !/full details/i.test(F.body));
    rec('R10b', 'card meta text is grammatical (no "1 steps")', !F.meta.some(m => /\b1 (steps|ingredients)\b/.test(m)), F.meta.join(' | '));
    await shot('03_cards');
    await b.ev(`document.querySelectorAll('#log .chips .chip')[0].click()`); await sleep(500);
    await b.waitFor(WAIT_IDLE, 60000);
    rec('R11', 'chip "Show me more" appends more recipe cards', await b.ev(`document.querySelectorAll('#log .card').length > ${F.n}`));
    await b.ev(`document.querySelector('#log .card').click()`); await sleep(500);
    await b.waitFor(WAIT_IDLE, 60000);
    const page = await b.ev(`JSON.stringify((()=>{const r=${lastBot};return {h:[...r.querySelectorAll('h3,h5')].map(h=>h.innerText), li:r.querySelectorAll('li').length, txt:r.innerText}})())`);
    const P = JSON.parse(page);
    rec('R12', 'card click opens recipe page: title, Ingredients, Method sections', P.h.includes('Ingredients') && P.h.includes('Method') && P.li > 5 && !/\(the step is empty|\bnull\b|undefined/i.test(P.txt), JSON.stringify(P.h));
    await shot('04_recipe_page');

    // ---------------------------------------------------------------- R13-R15 sidebar: New chat, History, My Nosh, Help
    const histBefore = await b.ev(`JSON.parse(localStorage.getItem('nosh_hist')||'[]').length`);
    await b.ev(`document.querySelector('#new').click()`); await sleep(900);
    const afterNew = await b.ev(`JSON.stringify({rows:document.querySelectorAll('#log .row').length, welcome:document.querySelector('#log .row .bubble').innerText.includes("Hello! I'm Nosh."), hist:JSON.parse(localStorage.getItem('nosh_hist')||'[]')})`);
    const AN = JSON.parse(afterNew);
    rec('R13', 'New chat: conversation cleared, welcome shown, first question saved to History', AN.rows === 1 && AN.welcome && AN.hist.length >= histBefore && AN.hist[0].toLowerCase().includes('penne'), afterNew);
    await b.ev(`document.querySelector('#histBtn').click()`); await sleep(200);
    const hopen = await b.ev(`JSON.stringify({open:document.querySelector('#hist').classList.contains('open'), items:[...document.querySelectorAll('#histList li')].map(l=>l.innerText)})`);
    await shot('05_history_open');
    await b.ev(`document.querySelector('#histList li').click()`); await sleep(500);
    const replay = await b.waitFor(`document.querySelectorAll('#log .row.me').length===1 && ${WAIT_IDLE}`, 60000);
    rec('R14', 'History: expands, lists past chats, clicking one starts a new chat and replays it', JSON.parse(hopen).open && JSON.parse(hopen).items.length >= 1 && replay, hopen);
    await b.ev(`document.querySelector('#histBtn').click()`);
    await b.ev(`document.querySelector('#help').click()`); await sleep(500);
    const help = await b.ev(`JSON.stringify({help:!document.querySelector('#helpView').hidden, chat:document.querySelector('#chatView').hidden, active:document.querySelector('#help').classList.contains('active'),
      txt:document.querySelector('#helpView').innerText, tel:[...document.querySelectorAll('#helpView a[href^="tel:"]')].length, mail:[...document.querySelectorAll('#helpView a[href^="mailto:"]')].length,
      faq:document.querySelectorAll('#helpView .hrow').length, maps:!!document.querySelector('#helpView a[href*="google.com/maps"]'), pageScroll:document.documentElement.scrollHeight>innerHeight+1})`);
    const H = JSON.parse(help);
    rec('R15', 'Help & Support page: contact cards (phone/email/address), response time, 3 FAQ links, warranty, assistant examples; chat hidden; nav highlighted',
      H.help && H.chat && H.active && ['+91 63663 77918', 'hello@euphotic.io', 'Brookefield', '15 days', 'Wi-Fi connectivity', 'App settings', 'Routine maintenance', 'up to 2 years', 'Back to chat'].every(t => H.txt.includes(t)) && H.tel >= 1 && H.mail >= 1 && H.faq === 3 && H.maps && !H.pageScroll, JSON.stringify({ tel: H.tel, mail: H.mail, faq: H.faq }));
    await shot('06_help');
    await b.ev(`document.querySelector('#helpView .hchips button').click()`); await sleep(600);
    const fromHelp = await b.waitFor(`document.querySelector('#helpView').hidden && !document.querySelector('#chatView').hidden && document.querySelectorAll('#log .row.me').length>=1 && ${WAIT_IDLE}`, 60000);
    rec('R16', 'Help example button returns to chat and runs that example', fromHelp);
    await b.ev(`document.querySelector('#help').click()`); await sleep(300); await b.ev(`document.querySelector('#helpBack').click()`); await sleep(300);
    rec('R17', 'Help "Back to chat" restores chat view and un-highlights nav', await b.ev(`document.querySelector('#helpView').hidden && !document.querySelector('#chatView').hidden && !document.querySelector('#help').classList.contains('active')`));
    await b.ev(`document.querySelector('#help').click()`); await sleep(200); await b.ev(`document.querySelector('#new').click()`); await sleep(500);
    rec('R18', 'New chat from the Help page returns to a fresh chat', await b.ev(`!document.querySelector('#chatView').hidden && document.querySelector('#helpView').hidden && document.querySelectorAll('#log .row').length===1`));

    // ---------------------------------------------------------------- R19-R23 documents
    const mk = `(()=>{const dt=new DataTransfer();
      dt.items.add(new File(["# Nosh Warranty\\n\\n## Returns\\nCustomers may return the machine within 30 days for a full refund. A 10% restocking fee applies to opened units.\\n\\n## Warranty\\nThe machine has a 2 year limited warranty. Water damage is not covered.\\n"],'policy.md',{type:'text/markdown'}));
      dt.items.add(new File(["month,region,revenue\\nJan,North,120000\\nJan,South,90000\\nFeb,North,150000\\nFeb,South,70000\\n"],'sales.csv',{type:'text/csv'}));
      dt.items.add(new File([new Uint8Array(3000).map((_,i)=>(i*37)%251)],'junk.bin',{type:'application/octet-stream'}));
      const inp=document.querySelector('#file'); inp.files=dt.files; inp.dispatchEvent(new Event('change')); return dt.files.length})()`;
    await b.ev(mk);
    await b.waitFor(`document.querySelectorAll('.dchip').length>=2 && !document.querySelector('.dchip.read') && !document.querySelector('.dchip:not(.err) .bar')`, 60000);
    await sleep(1500);
    const docs = await b.ev(`JSON.stringify({chips:[...document.querySelectorAll('.dchip:not(.err)')].map(c=>c.innerText.replace(/\\n+/g,' | ')), ph:document.querySelector('#q').placeholder, cnt:document.querySelector('#attach .cnt').innerText,
      cntVisible:!document.querySelector('#attach .cnt').hidden, log:document.querySelector('#log').innerText, hasBadge:[...document.querySelectorAll('.dchip .ft')].map(x=>x.innerText)})`);
    const D = JSON.parse(docs);
    rec('R19', 'uploads: tiles with type badge/size/counts, paperclip count, placeholder names the files, chat confirms each file',
      D.chips.length === 2 && D.hasBadge.includes('MD') && D.hasBadge.includes('CSV') && D.cnt === '2' && D.cntVisible && D.ph.includes('your 2 documents') &&
      D.log.includes("I've read policy.md") && D.log.includes("I've read sales.csv"), docs);
    rec('R20', 'unsupported upload (.bin) is rejected with a clear chat message and is not attached', D.log.includes("couldn't add") && D.log.includes('junk.bin') && !D.chips.some(c => c.includes('junk.bin')));
    await shot('07_uploads');
    await ask('What is the return window and is there a restocking fee?');
    const dq = await b.ev(`JSON.stringify((()=>{const r=${lastBot};return {t:r.querySelector('.bubble').innerText, src:[...r.querySelectorAll('.sources em')].map(e=>e.innerText), cites:r.querySelectorAll('sup.cite').length}})())`);
    const DQ = JSON.parse(dq);
    rec('R21', 'document Q&A through the UI: correct facts, citation badges, source chips', /30 days/.test(DQ.t) && /10\s?%/.test(DQ.t) && DQ.src.some(s => s.includes('policy.md')) && DQ.cites >= 1, dq);
    await shot('08_doc_answer');
    await ask('Which region had the highest total revenue?');
    const sq = await b.ev(`(()=>{const r=${lastBot};return r.querySelector('.bubble').innerText})()`);
    rec('R22', 'table question computed correctly (North = 270,000)', /North/.test(sq) && /270,?000/.test(sq), sq.slice(0, 150));
    await ask('What does the policy say about shipping costs to Mars?');
    const nf = await b.ev(`(()=>{const r=${lastBot};return r.querySelector('.bubble').innerText})()`);
    rec('R23', 'unanswerable document question: says it is not in the documents (no invention)', /(couldn'?t find|not (?:mention|cover|contain|include|in)|no (?:mention|information)|doesn'?t (?:say|mention|contain|cover)|isn'?t)/i.test(nf) && !/\$\s?\d|mars.*(free|costs? \d)/i.test(nf), nf.slice(0, 200));
    await b.ev(`document.querySelector('.dchip .x').click()`); await sleep(900);
    rec('R24', 'removing an attachment (x) updates tiles, badge and placeholder', await b.ev(`document.querySelectorAll('.dchip').length===1 && document.querySelector('#attach .cnt').innerText==='1' && document.querySelector('#q').placeholder.startsWith('Ask about ')`));
    await b.goto(BASE + '/');
    rec('R25', 'attachments persist across a page reload', await b.ev(`document.querySelectorAll('.dchip').length===1`));
    await b.ev(`(()=>{const dt=new DataTransfer();dt.items.add(new File(['x'],'a.txt'));const e=new DragEvent('dragenter',{dataTransfer:dt,bubbles:true,cancelable:true});document.dispatchEvent(e)})()`); await sleep(200);
    const dropOn = await b.ev(`document.querySelector('#drop').classList.contains('on')`);
    await shot('09_drop_overlay');
    await b.ev(`document.dispatchEvent(new DragEvent('dragleave',{bubbles:true}))`); await sleep(200);
    rec('R26', 'drag & drop overlay appears on drag and disappears on leave', dropOn && await b.ev(`!document.querySelector('#drop').classList.contains('on')`));
    await resetDocs();

    // ---------------------------------------------------------------- R27-R29 scrolling & overflow
    await b.goto(BASE + '/');
    await b.ev(`document.querySelector('#new').click()`); await sleep(500);
    await ask('Explain in detail how to make a classic roux for gravy with a table comparing white, blond and brown roux, a short python code block converting cups to ml, and bullet tips.', 90000);
    await sleep(500);
    const sc = await b.ev(`JSON.stringify((()=>{const log=document.querySelector('#log');const r=${lastBot};const lr=log.getBoundingClientRect(),rr=r.getBoundingClientRect();
      return {answerTopVisible:rr.top>=lr.top-3&&rr.top<lr.bottom, scrollable:log.scrollHeight>log.clientHeight, pageScroll:document.documentElement.scrollHeight>innerHeight+1,
      jump:document.querySelector('#jump').classList.contains('on'), tableOverflow:[...r.querySelectorAll('table,pre')].some(e=>e.scrollWidth>e.parentElement.clientWidth+1),
      bubbleOverflow:r.querySelector('.bubble').scrollWidth>r.querySelector('.bubble').clientWidth+1, hasTable:!!r.querySelector('table'), hasCode:!!r.querySelector('pre code'), formVisible:document.querySelector('#f').getBoundingClientRect().bottom<=innerHeight}})())`);
    const SC = JSON.parse(sc);
    rec('R27', 'long answer: first line visible, chat scrolls, page does not, composer stays visible, "Latest" button shown', SC.answerTopVisible && SC.scrollable && !SC.pageScroll && SC.jump && SC.formVisible, sc);
    rec('R28', 'long answer: table/code/bullets render without clipping or overflow', SC.hasTable && SC.hasCode && !SC.tableOverflow && !SC.bubbleOverflow, sc);
    await b.ev(`document.querySelector('#jump').click()`); await sleep(900);
    rec('R29', '"Latest" button scrolls to the end of the answer', await b.ev(`(()=>{const l=document.querySelector('#log');return l.scrollHeight-l.scrollTop-l.clientHeight<8})()`));
    await shot('10_long_answer');

    // ---------------------------------------------------------------- R30-R32 viewports, assets, auto-send param
    for (const [w, h] of [[1280, 720], [1366, 768], [1920, 1080]]) {
      await b.size(w, h); await sleep(300);
      const v = await b.ev(`JSON.stringify({h:document.documentElement.scrollWidth>innerWidth+1, p:document.documentElement.scrollHeight>innerHeight+1, f:document.querySelector('#f').getBoundingClientRect().bottom<=innerHeight})`);
      const V = JSON.parse(v); rec('R30', `viewport ${w}x${h}: no page scroll/overflow, composer visible`, !V.h && !V.p && V.f, v);
    }
    await b.size(1366, 768);
    const assets = await b.ev(`Promise.all(['/favicon.ico','/static/img/favicon.png','/static/img/avatar.jpg','/static/img/logo-footer.png','/static/img/dish_f2m.jpg','/static/img/dish_f3r.jpg','/static/img/dish_f1l.jpg','/static/img/dish_f1r.jpg','/static/img/dish_f2l.jpg','/static/img/dish_f2r.jpg','/static/img/dish_f3l.jpg','/static/img/dish_f3m.jpg'].map(u=>fetch(u).then(r=>r.status))).then(a=>JSON.stringify(a))`);
    rec('R31', 'all brand assets (logo, favicon, avatar, 8 dish photos) are served', JSON.parse(assets).every(s => s === 200), assets);
    await b.goto(BASE + '/?q=' + encodeURIComponent('How many recipes contain tomato?'), 1500);
    rec('R32', '?q= parameter auto-sends the message', await b.waitFor(`document.querySelectorAll('#log .row.me').length===1 && ${WAIT_IDLE} && /\\d/.test(${lastBot}.innerText)`, 60000));
    // double-submit guard
    await b.goto(BASE + '/'); await sleep(500);
    await b.ev(`(()=>{const i=document.querySelector('#q');i.value='hello there';document.querySelector('#f').requestSubmit();i.value='second message';document.querySelector('#f').requestSubmit()})()`);
    await b.waitFor(WAIT_IDLE, 60000); await sleep(300);
    rec('R33', 'busy guard: a second message sent while one is generating is ignored (one user bubble)', await b.ev(`document.querySelectorAll('#log .row.me').length===1`));
  } catch (e) {
    rec('R99', 'suite crashed', false, e.stack || e.message);
  }
  const unexpected = b.failedReqs.filter(u => !/400 .*\/api\/upload$/.test(u));   // the 400 is the server correctly rejecting the deliberate .bin upload (R20)
  rec('R00', 'no console errors / unexpected failed network requests during the whole run', b.errors.length === 0 && unexpected.length === 0, JSON.stringify({ errors: b.errors.slice(0, 3), failed: unexpected.slice(0, 5) }));
  await b.close();
  const pass = results.filter(r => r.ok).length;
  console.log(`\n${pass}/${results.length} UI checks passed`);
  if (outFile) fs.writeFileSync(outFile, JSON.stringify({ when: new Date().toISOString(), pass, total: results.length, results }, null, 1));
  process.exit(pass === results.length ? 0 : 1);
})();
