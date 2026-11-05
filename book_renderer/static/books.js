(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const state={csrf:'',books:[],jobs:[],bookId:'',book:null,checked:new Set(),autoMissing:true,revision:'',optionsRevision:'',settingsBook:'',request:0,editor:null,metadataEditor:null,libraryKey:'',galleryKey:'',chapterKey:'',uploading:false};
  const statusNames={queued:'В очереди',importing:'Извлечение текста',ready:'Готово',running:'Обрабатывается',completed:'Готово',failed:'Ошибка',cancelled:'Отменено',interrupted:'Остановлено',resource_paused:'Остановлено защитой ресурсов',missing:'Не озвучена',stale:'Текст изменён — нужна озвучка',pending:'Ожидает озвучки',rendering:'Озвучивается',publication_pending:'MP3 ожидает публикации'};
  const node=(tag,text,className='')=>{const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(className)item.className=className;return item;};
  const notice=message=>{$('notice').textContent=message;$('notice').hidden=!message;};
  const minutes=seconds=>{const value=Math.round(seconds);return `${Math.floor(value/60)}:${String(value%60).padStart(2,'0')}`;};
  const busy=()=>state.jobs.some(job=>job.book_id===state.bookId&&['queued','running','publication_pending'].includes(job.status));
  state.desktop=null;
  state.coverSaves=new Map();
  function openDeviceSettings(){
    const dialog=$('device-settings'),settings=state.desktop;if(!dialog||!settings)return;
    const cpuOnly=Array.isArray(settings.available_devices)&&!settings.available_devices.includes('cuda');
    $('device-hardware').textContent=`${settings.hardware.logical_cpus} потоков процессора · ${Math.round(settings.hardware.ram_total_bytes/1024**3)} GiB памяти${cpuOnly?' · CPU-сборка':''}`;
    $('device-cuda').closest('.device-choice').hidden=cpuOnly;
    $('device-gpu-description').textContent=settings.hardware.gpu_reason;
    $('device-cuda').disabled=!settings.hardware.cuda_available;
    const recommended=settings.recommended_device||'cpu';
    const device=settings.configured?settings.device:recommended;
    $('device-cpu').checked=device==='cpu';$('device-cuda').checked=device==='cuda';
    $('device-settings-close').hidden=!settings.configured;
    $('device-settings-message').textContent=state.jobs.some(job=>['queued','running'].includes(job.status))?'Изменить устройство можно после завершения или отмены задач.':'';
    $('device-settings-save').disabled=state.jobs.some(job=>['queued','running'].includes(job.status));
    if(!dialog.open)dialog.showModal();
  }
  const buttonTips={
    'Открыть':'Открыть книгу и её главы.',
    'Изменить текст':'Открыть книгу и перейти к списку глав для редактирования.',
    'Название, автор и обложка':'Изменить название, автора или обложку книги.',
    'Удалить':'Удалить книгу вместе с исходным файлом, текстами и всеми MP3 после подтверждения.',
    'Проверить текст':'Открыть текст главы для просмотра и исправления.',
    'Слушать':'Воспроизвести MP3 главы во встроенном плеере.',
    'Послушать':'Воспроизвести короткий образец выбранного голоса.',
    'Озвучить':'Озвучить эту главу с выбранными настройками.',
    'Переозвучить':'Заново озвучить эту главу; новый MP3 заменит предыдущий после успеха.'
  };
  const button=(text,handler,className='quiet')=>{const item=node('button',text,className);item.type='button';if(buttonTips[text])item.title=buttonTips[text];item.addEventListener('click',()=>action(handler));return item;};
  async function api(path,options={}) {
    const headers=new Headers(options.headers);
    if(options.method==='POST')headers.set('X-CSRF-Token',state.csrf);
    if(options.body&&!(options.body instanceof FormData)&&!headers.has('Content-Type'))headers.set('Content-Type','application/json');
    const response=await fetch(`/api/${path}`,{...options,headers,credentials:'same-origin',redirect:'error'});
    const data=await response.json().catch(()=>({error:`HTTP ${response.status}`}));
    if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);
    return data;
  }
  async function postForm(path,form) {
    const options={method:'POST',body:form};
    if(window.location.origin==='http://wails.localhost'){
      // WebView2's intercepted request stream can omit file-backed multipart data.
      // Serialize the entire form before fetch so Wails receives bytes, not a file reference.
      // Keep the browser-generated boundary and filename encoding together with the body.
      const multipart=new Response(form);
      options.headers={'Content-Type':multipart.headers.get('Content-Type')};
      options.body=await multipart.arrayBuffer();
    }
    return api(path,options);
  }
  async function uploadBook(file) {
    if(!file.size)throw new Error('Файл пуст.');
    if(file.size>100*1024**2)throw new Error('Книга превышает 100 MiB.');
    const form=new FormData();form.append('file',file);
    return postForm('books/upload',form);
  }
  function showCover(image,url,title) {
    if(image.dataset.coverUrl!==(url||'')||(url&&image.hidden)){
      if(url)image.src=url;else image.removeAttribute('src');
      image.dataset.coverUrl=url||'';
    }
    image.alt=title;image.hidden=!url;
  }
  async function action(callback) {try{notice('');await callback();await pollOnce();}catch(error){notice(error.message);}}
  function renderLibrary() {
    renderGallery();
    const key=JSON.stringify([state.books,state.bookId]);if(key===state.libraryKey)return;state.libraryKey=key;
    $('book-count').textContent=String(state.books.length);$('library').replaceChildren();
    if(!state.books.length)$('library').append(node('p','Здесь пока нет книг.','muted small'));
    for(const book of state.books){
      const item=button('',()=>selectBook(book.id),'library-item'+(book.id===state.bookId?' active':''));item.title='Открыть книгу и её главы.';
      item.append(node('strong',book.title),node('small',`${statusNames[book.status]||book.status} · ${book.audio_count||0} из ${book.chapters} глав готовы`));$('library').append(item);
    }
  }
  function renderGallery() {
    const query=$('book-search').value.trim().toLocaleLowerCase('ru-RU'),filter=$('book-filter').value;
    const active=new Set(state.jobs.filter(job=>['queued','running','publication_pending'].includes(job.status)).map(job=>job.book_id));
    const key=JSON.stringify([state.books,query,filter,[...active]]);if(key===state.galleryKey)return;state.galleryKey=key;
    const books=state.books.filter(book=>`${book.title} ${book.author} ${book.filename}`.toLocaleLowerCase('ru-RU').includes(query))
      .filter(book=>filter==='all'||filter==='audio'&&book.audio_count>0||filter==='ready'&&book.status==='ready'||filter==='attention'&&(['failed','cancelled','interrupted','resource_paused'].includes(book.status)||book.status==='ready'&&book.audio_count<book.chapters));
    $('gallery-count').textContent=`${books.length} из ${state.books.length}`;$('gallery').replaceChildren();$('empty').hidden=state.books.length>0;
    if(!books.length&&state.books.length)$('gallery').append(node('p','По этому запросу книг нет.','muted'));
    for(const book of books){
      const card=node('article',undefined,'book-card'),cover=button('',()=>selectBook(book.id),'book-cover');cover.setAttribute('aria-label',`Открыть книгу: ${book.title}`);cover.title='Открыть книгу и её главы.';
      const coverImage=node('img',undefined,'gallery-cover-image');coverImage.src=book.cover_url||'/static/logo.svg';coverImage.alt='';coverImage.addEventListener('error',()=>{if(!coverImage.src.endsWith('/static/logo.svg'))coverImage.src='/static/logo.svg';});
      const coverText=node('span',undefined,'gallery-cover-text');coverText.append(node('span',book.format.toUpperCase(),'cover-format'),node('strong',book.title),node('small',book.author||'Автор не указан'));cover.append(coverImage,coverText);
      const content=node('div',undefined,'card-content');content.append(node('h3',book.title),node('p',`${statusNames[book.status]||book.status} · ${book.audio_count||0} из ${book.chapters} глав готовы`,'muted small'));
      const actions=node('div',undefined,'card-actions');
      for(const [text,handler] of [['Открыть',()=>selectBook(book.id)],['Изменить текст',async()=>{await selectBook(book.id);$('chapters-panel').scrollIntoView({behavior:'smooth',block:'start'});}],['Название, автор и обложка',()=>openMetadata(book.id)],['Переозвучить',async()=>{await selectBook(book.id);selectChapters('all');$('render-selected').focus();notice('Выделены все главы. «Озвучить выделенные» заменит их MP3. Для продолжения выбери только неозвученные.');}]]){
        const item=button(text,handler);if(text==='Переозвучить')item.title='Открыть книгу и выделить все главы для повторной озвучки.';item.disabled=text!=='Открыть'&&(book.status!=='ready'||active.has(book.id));actions.append(item);
      }
      const remove=button('Удалить',()=>removeBook(book),'quiet danger');remove.disabled=active.has(book.id);actions.append(remove);content.append(actions);card.append(cover,content);$('gallery').append(card);
    }
  }
  function showGallery() {state.bookId='';state.book=null;state.request++;$('book-panel').hidden=true;$('gallery-panel').hidden=false;renderLibrary();}
  async function selectBook(id) {
    if(state.bookId!==id){state.bookId=id;state.book=null;state.checked=new Set();state.autoMissing=true;state.revision='';state.optionsRevision='';state.chapterKey='';$('book-panel').hidden=true;}
    await loadBook();renderLibrary();
  }
  async function loadBook() {
    if(!state.bookId)return;
    const id=state.bookId,sequence=++state.request,book=await api(`books/${id}`);
    if(id!==state.bookId||sequence!==state.request)return;
    state.book=book;
    if(state.settingsBook!==id){
      const settings=book.render_settings||{};$('voice').value=settings.voice||'xenia';$('rate').value=String(settings.rate||1);$('pitch').value=String(settings.pitch||0);state.settingsBook=id;
    }
    if(state.revision!==book.document_revision){state.autoMissing=true;state.revision=book.document_revision;}
    if(state.autoMissing)state.checked=new Set(book.chapter_list.filter(chapter=>!chapter.ready).map(chapter=>chapter.index));
    else state.checked=new Set([...state.checked].filter(index=>book.chapter_list.some(chapter=>chapter.index===index)));
    renderBook();
  }
  function selectChapters(mode) {
    if(!state.book)return;
    state.autoMissing=mode==='missing';state.checked=new Set(state.book.chapter_list.filter(chapter=>mode==='all'||mode==='missing'&&!chapter.ready).map(chapter=>chapter.index));renderBook();
  }
  function selectionSummary() {
    const chapters=state.book.chapter_list,ready=chapters.filter(chapter=>chapter.ready).length;
    $('chapter-summary').textContent=`Готово ${ready} из ${chapters.length}. Не озвучено / требует обновления: ${chapters.length-ready}. Выделено: ${state.checked.size}.`;
    $('render-selected').disabled=state.book.status!=='ready'||busy()||!state.checked.size;
  }
  function play(index,title,sha) {
    $('audio-player').src=`/api/books/${state.bookId}/audio/${index}?v=${encodeURIComponent(sha||'')}`;
    $('audio-player').dataset.bookId=state.bookId;$('player-title').textContent=title;$('player-subtitle').textContent=state.book.title;
    $('audio-player').play().catch(()=>notice('Нажми кнопку воспроизведения в плеере.'));
  }
  function chapterSignature() {return JSON.stringify([state.book.document_revision,state.book.chapter_list,[...state.checked].sort((a,b)=>a-b),busy()]);}
  function renderChapters() {
    selectionSummary();const key=chapterSignature();if(key===state.chapterKey)return;state.chapterKey=key;
    $('chapters').replaceChildren();
    for(const chapter of state.book.chapter_list){
      const row=node('div',undefined,'chapter chapter-'+chapter.status);row.id=`chapter-${chapter.index}`;
      const checkbox=node('input');checkbox.type='checkbox';checkbox.checked=state.checked.has(chapter.index);checkbox.setAttribute('aria-label',`Выбрать: ${chapter.title}`);checkbox.title='Выбрать главу для озвучки; готовая выбранная глава будет озвучена заново.';
      checkbox.addEventListener('change',()=>{state.autoMissing=false;if(checkbox.checked)state.checked.add(chapter.index);else state.checked.delete(chapter.index);selectionSummary();state.chapterKey=chapterSignature();});
      const description=node('div');description.append(node('strong',`${chapter.index}. ${chapter.title}`));
      let detail=`${chapter.characters.toLocaleString('ru-RU')} знаков · ${statusNames[chapter.status]||chapter.status}`;
      if(chapter.duration)detail+=` · ${minutes(chapter.duration)}`;
      if(chapter.available&&chapter.settings.voice)detail+=` · ${chapter.settings.voice} · ${chapter.settings.rate}×`;
      description.append(node('small',detail));if(chapter.error)description.append(node('p',chapter.error,'chapter-error'));
      const actions=node('div',undefined,'chapter-actions');
      const edit=button('Проверить текст',()=>editChapter(chapter.index));edit.disabled=busy();actions.append(edit);
      if(chapter.available){
        actions.append(button('Слушать',()=>play(chapter.index,chapter.title,chapter.sha256)));
        const download=node('a','MP3','button');download.title='Сохранить MP3 этой главы на компьютер.';download.href=`/api/books/${state.bookId}/audio/${chapter.index}?download=1&v=${encodeURIComponent(chapter.sha256)}`;
        download.addEventListener('click',event=>{if(window.go?.main?.Desktop?.SaveAudio){event.preventDefault();action(async()=>{if(await window.go.main.Desktop.SaveAudio(state.bookId,chapter.index))notice('MP3 сохранён.');});}});actions.append(download);
      }
      const generate=button(chapter.available?'Переозвучить':'Озвучить',()=>requestRender(false,[chapter.index]));generate.disabled=busy()||state.book.status!=='ready';actions.append(generate);
      row.append(checkbox,description,actions);$('chapters').append(row);
    }
    if(!state.book.chapter_list.length)$('chapters').append(node('p','Главы появятся после извлечения текста.','muted'));
  }
  function renderBook() {
    const book=state.book;if(!book)return;
    $('gallery-panel').hidden=true;$('book-panel').hidden=false;
    $('book-title').textContent=book.title;$('book-author').textContent=book.author||'ТВОЯ ЛОКАЛЬНАЯ БИБЛИОТЕКА';
    showCover($('book-cover-image'),book.cover?.url,`Обложка: ${book.title}`);
    $('book-meta').textContent=`${book.filename} · ${book.chapter_list.length} глав`;
    const warnings=[...(book.warnings||[])];if(book.error)warnings.push(book.error);$('book-warnings').replaceChildren(...warnings.map(text=>node('p',text)));$('book-warnings').hidden=!warnings.length;
    const disabled=book.status!=='ready'||busy();for(const id of ['preview','render-all','cleanup-notes','edit-metadata','open-pronunciation'])$(id).disabled=disabled;
    const pronunciation=book.pronunciation||{};$('pronunciation-count').textContent=`Замен: ${(pronunciation.replacements||[]).length} · ударений: ${(pronunciation.stresses||[]).length}`;
    for(const id of ['reimport','delete-book'])$(id).disabled=busy();
    if(state.optionsRevision!==book.document_revision){
      const options=book.import_options||{};$('encoding').value=options.encoding||'utf-8-sig';
      for(const [id,key,fallback] of [['force-ocr','force_ocr',false],['remove-headers','remove_headers',true],['clean-noise','clean_noise',true],['skip-references','skip_references',true],['remove-editor-notes','remove_editor_notes',true],['include-notes','include_notes',false]])$(id).checked=options[key]??fallback;
      $('skip-references').disabled=!$('clean-noise').checked;state.optionsRevision=book.document_revision;
    }
    const ready=book.chapter_list.filter(chapter=>chapter.ready).length;
    $('export').classList.toggle('disabled',!ready||busy());$('export').setAttribute('aria-disabled',String(!ready||busy()));
    if(ready&&!busy())$('export').href=`/api/books/${state.bookId}/export`;else $('export').removeAttribute('href');
    $('audio-directory').textContent=book.audio_directory;
    $('preview-audio').replaceChildren();$('preview-audio').hidden=!book.preview;
    if(book.preview)$('preview-audio').append(node('span','Короткий образец'),button('Послушать',()=>play(0,'Образец голоса',book.preview.sha256)));
    renderChapters();
    $('excluded').textContent=book.excluded?.length?'Исключённый текст (первые 100 записей):\n'+book.excluded.map(entry=>`${entry.page?'Стр. '+entry.page:'Раздел до очистки '+entry.chapter} · ${entry.reason}: ${entry.text}`).join('\n\n'):'';
    const cleanup=book.cleanup||{},details=[];
    if(cleanup.removed_lines)details.push(`Исключено строк: ${cleanup.removed_lines}; целых страниц: ${cleanup.removed_pages||0}.`);
    if(cleanup.removed_notes)details.push(`Удалено примечаний: ${cleanup.removed_notes}.`);
    if(cleanup.removed_markers)details.push(`Убрано маркеров: ${cleanup.removed_markers}.`);
    if(cleanup.removed_page_numbers)details.push(`Убрано номеров страниц: ${cleanup.removed_page_numbers}.`);
    if(cleanup.removed_running_headers)details.push(`Убрано колонтитулов: ${cleanup.removed_running_headers}.`);
    $('cleanup-summary').textContent=details.length?details.join(' '):'Исключений очистки пока нет.';
    $('cleanup-report').hidden=!book.excluded_count;if(book.excluded_count)$('cleanup-report').href=`/api/books/${state.bookId}/cleanup-report`;
    const job=state.jobs.find(item=>item.book_id===state.bookId&&(item.kind!=='render'||item.document_revision===book.document_revision));
    $('job-panel').hidden=!job;
    if(job){
      const stopped=['failed','cancelled','interrupted','resource_paused'].includes(job.status);
      const lastPhase=job.progress.failed_phase||job.progress.phase;
      $('job-phase').textContent=stopped?`${statusNames[job.status]}${lastPhase?' · '+lastPhase:''}`:job.status==='publication_pending'?statusNames[job.status]:job.progress.phase||statusNames[job.status]||job.status;$('job-progress').value=Number(job.progress.percent)||0;
      const lines=[job.error||statusNames[job.status]||job.status];
      if(stopped)lines.push(job.kind==='cover'?'Обложку сохранить не удалось. Открой «Название, автор и обложка» и выбери картинку ещё раз.':job.kind==='import'?'Извлечение текста не завершено. После устранения причины нажми «Извлечь текст заново».':'Исправь проблемную главу, нажми «Выделить неозвученные» → «Озвучить выделенные».');
      if(job.progress.text_sanitized_chapters)lines.push(`Глав с очищенной разметкой: ${job.progress.text_sanitized_chapters}.`);
      if(job.progress.text_fallbacks)lines.push(`Фрагментов после дополнительной очистки: ${job.progress.text_fallbacks}.`);
      if(job.progress.removed_editor_notes)lines.push(`Не озвучено примечаний: ${job.progress.removed_editor_notes}.`);
      if(job.progress.nonspoken_chapters?.length)lines.push(`Разделы-паузы: ${job.progress.nonspoken_chapters.join(', ')}.`);
      if(job.progress.removed_page_numbers)lines.push(`Пропущено номеров страниц: ${job.progress.removed_page_numbers}.`);
      if(job.progress.removed_running_headers)lines.push(`Пропущено колонтитулов: ${job.progress.removed_running_headers}.`);
      $('job-detail').textContent=lines.join('\n');$('cancel-job').hidden=!['queued','running','publication_pending'].includes(job.status);$('cancel-job').dataset.job=job.id;
      $('retry-publication').hidden=job.status!=='publication_pending';$('retry-publication').dataset.job=job.id;
      if($('retry-import'))$('retry-import').hidden=!stopped||job.kind!=='import';
      if($('job-diagnostics')){
        $('job-diagnostics').hidden=!stopped||!job.diagnostics_directory;
        const diagnostics=[`Задача: ${job.id}`,`Операция: ${job.kind}`,`Последний этап: ${lastPhase||'нет отчёта'}`];
        if(job.progress.error_type)diagnostics.push(`Тип ошибки: ${job.progress.error_type}`);
        if(job.progress.error_winerror!==undefined)diagnostics.push(`WinError: ${job.progress.error_winerror}`);
        if(job.progress.error_errno!==undefined)diagnostics.push(`errno: ${job.progress.error_errno}`);
        if(job.progress.fragment!==undefined)diagnostics.push(`Фрагмент: ${job.progress.fragment}; знаков после подготовки: ${job.progress.fragment_characters??'неизвестно'}`);
        if(job.progress.worker_pid!==undefined)diagnostics.push(`PID обработчика: ${job.progress.worker_pid}`);
        if(job.progress.worker_exit_code!==undefined&&job.progress.worker_exit_code!==null)diagnostics.push(`Код завершения: ${job.progress.worker_exit_code} (0x${(Number(job.progress.worker_exit_code)>>>0).toString(16).padStart(8,'0')})`);
        if(job.progress.tool)diagnostics.push(`Инструмент: ${job.progress.tool}`);
        if(job.progress.tool_exit_code!==undefined)diagnostics.push(`Код инструмента: ${job.progress.tool_exit_code}`);
        if(job.progress.tool_timeout_seconds!==undefined)diagnostics.push(`Тайм-аут инструмента: ${job.progress.tool_timeout_seconds} с`);
        if(job.progress.tool_stderr)diagnostics.push(`Сообщение инструмента:\n${job.progress.tool_stderr}`);
        diagnostics.push(`Папка журналов: ${job.diagnostics_directory||''}`, 'worker.log — этапы и ошибки обработчика; manager.log — очередь и публикация; host.log — запуск, ресурсы и изоляция; host-report.json — итог ограничений и завершения; failure.json — отчёт при ошибке (если удалось сохранить).');
        $('job-diagnostics-text').textContent=diagnostics.join('\n');
      }
    }
  }
  async function requestRender(preview,chapters) {
    if(!state.book)return;
    if(state.desktop&&!state.desktop.configured){openDeviceSettings();return;}
    await api(`books/${state.bookId}/render`,{method:'POST',body:JSON.stringify({chapter_storage:2,document_revision:state.book.document_revision,voice:$('voice').value,rate:Number($('rate').value),pitch:Number($('pitch').value),preview,chapters})});
    notice(preview?'Образец поставлен в очередь.':'Выбранные главы поставлены в очередь. Готовый новый MP3 заменит прежний файл этой главы.');
  }
  async function pollOnce() {
    const data=await api('state');state.csrf=data.csrf_token;state.books=data.books;state.jobs=data.jobs;$('upload-button').disabled=state.uploading;
    for(const [id] of state.coverSaves){
      const job=state.jobs.find(item=>item.id===id);if(!job||['queued','running'].includes(job.status))continue;
      state.coverSaves.delete(id);
      notice(job.status==='completed'?'Название, автор и обложка сохранены.':`Не удалось сохранить изменения обложки: ${job.error||statusNames[job.status]||job.status}`);
    }
    state.desktop=data.desktop_settings;
    if(state.desktop){
      $('device-title').textContent=!state.desktop.configured?'Выбери устройство озвучки':state.desktop.device==='cpu'?'Процессор · экономный режим':'Видеокарта NVIDIA';
      if(!state.desktop.configured&&!$('device-settings').open)openDeviceSettings();
      if($('device-settings').open)$('device-settings-save').disabled=state.jobs.some(job=>['queued','running'].includes(job.status));
    }
    $('resource-status').textContent=`CPU ≤ ${state.desktop?state.desktop.cpu_percent+'%':data.limits.cpu_quota_vcpus+' ядра'} · память обработчика ≤ ${Math.round(data.limits.ram_max_bytes/1024**3)} GiB · свободно ${Math.round(data.disk_free_bytes/1024**3)} GiB`;
    renderLibrary();if(state.bookId)await loadBook();
  }
  async function poll() {try{await pollOnce();}catch(error){notice('Локальный сервер недоступен: '+error.message);}setTimeout(poll,2000);}
  async function removeBook(book) {
    if(!confirm(`Удалить «${book.title}», её исходник, текст, все MP3 и журналы?`))return;
    await api(`books/${book.id}/delete`,{method:'POST',body:'{}'});
    if($('audio-player').dataset.bookId===book.id){$('audio-player').pause();$('audio-player').removeAttribute('src');$('audio-player').load();$('player-title').textContent='Готовые главы можно слушать здесь';$('player-subtitle').textContent='Выбери «Слушать» рядом с MP3.';}
    if(state.bookId===book.id)showGallery();notice('Книга и все её MP3 удалены.');
  }
  async function editChapter(index) {
    const data=await api(`books/${state.bookId}/chapters/${index}`);state.editor={bookId:state.bookId,index,revision:data.document_revision,originalTitle:data.chapter.title,originalText:data.chapter.text};
    $('chapter-title').value=data.chapter.title;$('chapter-text').value=data.chapter.text;$('editor-message').textContent='';$('editor').showModal();
  }
  function closeEditor() {
    if(state.editor&&($('chapter-title').value!==state.editor.originalTitle||$('chapter-text').value!==state.editor.originalText)&&!confirm('Закрыть без сохранения исправлений?'))return;
    $('editor').close();state.editor=null;
  }
  async function openMetadata(bookId) {
    const book=await api(`books/${bookId}`);if(book.status!=='ready')throw new Error('Сначала дождись готового текста книги.');
    state.metadataEditor={bookId,revision:book.document_revision,title:book.title,author:book.author,cover:book.cover,coverFile:null,coverUrl:null,saving:false};
    $('metadata-title').value=book.title;$('metadata-author').value=book.author;$('metadata-cover-file').value='';$('metadata-message').textContent='';renderMetadataCover();$('metadata').showModal();
  }
  function renderMetadataCover() {
    const edit=state.metadataEditor;if(!edit)return;
    const url=edit.coverUrl||edit.cover?.url;
    showCover($('metadata-cover-image'),url,'Обложка книги');$('metadata-cover-empty').hidden=!!url;
    $('metadata-cover-caption').textContent=edit.coverFile?'Новая картинка — нажми «Сохранить»':edit.cover?.default?'Логотип по умолчанию — можно добавить свою обложку':edit.cover?(edit.cover.custom?'Твоя обложка':'Текущая обложка книги'):'Можно добавить свою картинку';
    $('metadata-cover-reset').hidden=!edit.coverFile;
  }
  function resetCoverChoice(clearInput=true) {
    const edit=state.metadataEditor;if(!edit)return;
    if(edit.coverUrl)URL.revokeObjectURL(edit.coverUrl);
    edit.coverFile=null;edit.coverUrl=null;if(clearInput)$('metadata-cover-file').value='';renderMetadataCover();
  }
  function finishMetadata() {
    if(state.metadataEditor?.coverUrl)URL.revokeObjectURL(state.metadataEditor.coverUrl);
    showCover($('metadata-cover-image'),null,'Обложка книги');$('metadata-cover-file').value='';
    $('metadata').close();state.metadataEditor=null;
  }
  function closeMetadata() {
    const original=state.metadataEditor;if(original?.saving)return;
    if(original&&($('metadata-title').value!==original.title||$('metadata-author').value!==original.author||original.coverFile)&&!confirm('Закрыть без сохранения?'))return;
    finishMetadata();
  }
  $('upload-form').addEventListener('submit',event=>{
    event.preventDefault();if(state.uploading)return;
    action(async()=>{
      const file=$('book-file').files[0];if(!file)throw new Error('Выбери книгу.');
      state.uploading=true;$('upload-button').disabled=true;
      try{
        const result=await uploadBook(file);
        $('book-file').value='';await selectBook(result.book_id);
        if(result.duplicate)notice('Этот файл уже есть в библиотеке.');
      }finally{state.uploading=false;$('upload-button').disabled=false;}
    });
  });
  $('preview').addEventListener('click',()=>action(()=>requestRender(true,[])));
  if($('retry-import'))$('retry-import').addEventListener('click',()=>$('reimport').click());
  if($('device-settings')){
    $('open-device-settings').addEventListener('click',openDeviceSettings);
    $('device-settings-close').addEventListener('click',()=>{$('device-settings').close();});
    $('device-settings').addEventListener('cancel',event=>{if(!state.desktop?.configured)event.preventDefault();});
    $('device-settings-form').addEventListener('submit',async event=>{
      event.preventDefault();$('device-settings-save').disabled=true;
      try{const device=$('device-cuda').checked?'cuda':'cpu';state.desktop=await api('desktop/device',{method:'POST',body:JSON.stringify({device})});$('device-settings').close();notice('Устройство сохранено.');await pollOnce();}
      catch(error){$('device-settings-message').textContent=error.message;}
      finally{$('device-settings-save').disabled=false;}
    });
  }
  $('render-selected').addEventListener('click',()=>action(()=>requestRender(false,[...state.checked])));
  $('render-all').addEventListener('click',()=>action(()=>requestRender(false,state.book.chapter_list.map(chapter=>chapter.index))));
  $('select-missing').addEventListener('click',()=>selectChapters('missing'));
  $('toggle-selection').addEventListener('click',()=>selectChapters('all'));
  $('clear-selection').addEventListener('click',()=>selectChapters('none'));
  $('open-folder').addEventListener('click',()=>action(async()=>{const data=await api(`books/${state.bookId}/open-folder`,{method:'POST',body:'{}'});notice((data.opened?'Открыта папка MP3: ':'Папка MP3: ')+data.path);}));
  $('export').addEventListener('click',event=>{if(window.go?.main?.Desktop?.SaveArchive){event.preventDefault();if(!$('export').classList.contains('disabled'))action(async()=>{if(await window.go.main.Desktop.SaveArchive(state.bookId))notice('ZIP сохранён.');});}});
  $('cancel-job').addEventListener('click',()=>action(()=>api(`jobs/${$('cancel-job').dataset.job}/cancel`,{method:'POST',body:'{}'})));
  $('retry-publication').addEventListener('click',()=>action(()=>api(`jobs/${$('retry-publication').dataset.job}/publish`,{method:'POST',body:'{}'})));
  $('reimport').addEventListener('click',()=>action(async()=>{if(!confirm('Заново извлечь текст из оригинала? Ручные исправления заменятся; изменившимся главам понадобится новая озвучка.'))return;await api(`books/${state.bookId}/import`,{method:'POST',body:JSON.stringify({encoding:$('encoding').value,force_ocr:$('force-ocr').checked,remove_headers:$('remove-headers').checked,clean_noise:$('clean-noise').checked,skip_references:$('skip-references').checked,remove_editor_notes:$('remove-editor-notes').checked,include_notes:$('include-notes').checked})});}));
  $('cleanup-notes').addEventListener('click',()=>action(async()=>{if(!state.book)return;const book=state.book;$('cleanup-notes').disabled=true;const result=await api(`books/${book.id}/cleanup-notes`,{method:'POST',body:JSON.stringify({document_revision:book.document_revision})});notice(`Текст очищен без OCR. Удалено примечаний: ${result.removed_notes}; маркеров: ${result.removed_markers}; номеров страниц: ${result.removed_page_numbers||0}; колонтитулов: ${result.removed_running_headers||0}. Изменившимся главам нужна новая озвучка. Удалённое сохранено в отчёте.`);}));
  $('delete-book').addEventListener('click',()=>action(()=>removeBook(state.book)));
  $('show-gallery').addEventListener('click',showGallery);$('book-search').addEventListener('input',renderGallery);$('book-filter').addEventListener('change',renderGallery);
  $('clean-noise').addEventListener('change',()=>{$('skip-references').disabled=!$('clean-noise').checked;});
  $('edit-metadata').addEventListener('click',()=>action(()=>openMetadata(state.bookId)));
  $('open-pronunciation').addEventListener('click',()=>action(async()=>{
    const book=await api(`books/${state.bookId}`);
    await window.ZvukbookPronunciation.open(book,api,async()=>{await pollOnce();});
  }));
  $('metadata-close').addEventListener('click',closeMetadata);$('metadata').addEventListener('cancel',event=>{event.preventDefault();closeMetadata();});
  $('metadata-cover-file').addEventListener('change',()=>{
    const edit=state.metadataEditor;if(!edit||edit.saving)return;
    const file=$('metadata-cover-file').files[0];if(!file)return;
    resetCoverChoice(false);$('metadata-message').textContent='';
    if(!file.size||file.size>16*1024**2){$('metadata-cover-file').value='';$('metadata-message').textContent='Выбери непустую картинку не больше 16 MiB.';return;}
    edit.coverFile=file;edit.coverUrl=URL.createObjectURL(file);renderMetadataCover();
  });
  $('metadata-cover-reset').addEventListener('click',()=>{resetCoverChoice();$('metadata-message').textContent='';});
  $('metadata-cover-image').addEventListener('error',()=>{
    const edit=state.metadataEditor;if(!edit)return;
    if(edit.coverFile){resetCoverChoice();$('metadata-message').textContent='Не удалось открыть картинку. Выбери JPG, PNG или WebP.';}
    else{$('metadata-cover-image').hidden=true;$('metadata-cover-empty').hidden=false;}
  });
  $('metadata-cover-image').addEventListener('load',()=>{
    const image=$('metadata-cover-image');if(state.metadataEditor?.coverFile&&image.naturalWidth*image.naturalHeight>20_000_000){resetCoverChoice();$('metadata-message').textContent='Картинка превышает 20 млн пикселей. Выбери изображение поменьше.';}
  });
  $('book-cover-image').addEventListener('error',()=>{$('book-cover-image').hidden=true;});
  $('metadata-form').addEventListener('submit',async event=>{
    event.preventDefault();const edit=state.metadataEditor;if(!edit||edit.saving)return;
    const payload={document_revision:edit.revision,title:$('metadata-title').value,author:$('metadata-author').value};
    edit.saving=true;for(const element of $('metadata-form').elements)element.disabled=true;$('metadata-message').textContent='Сохраняем изменения…';
    try{
      let result;
      if(edit.coverFile){
        const form=new FormData();for(const [key,value] of Object.entries(payload))form.append(key,value);form.append('cover',edit.coverFile);
        result=await postForm(`books/${edit.bookId}/metadata`,form);
      }else result=await api(`books/${edit.bookId}/metadata`,{method:'POST',body:JSON.stringify(payload)});
      if(result.job_id)state.coverSaves.set(result.job_id,true);
      finishMetadata();notice(result.job_id?'Сохраняем новую обложку…':'Название и автор сохранены.');await selectBook(edit.bookId);await pollOnce();
    }catch(error){if(state.metadataEditor===edit)$('metadata-message').textContent=error.message;else notice(error.message);}
    finally{edit.saving=false;for(const element of $('metadata-form').elements)element.disabled=false;}
  });
  $('editor-close').addEventListener('click',closeEditor);$('editor').addEventListener('cancel',event=>{event.preventDefault();closeEditor();});
  $('editor-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{const edit=state.editor;if(!edit)return;$('editor-save').disabled=true;try{await api(`books/${edit.bookId}/chapter`,{method:'POST',body:JSON.stringify({document_revision:edit.revision,index:edit.index,title:$('chapter-title').value,text:$('chapter-text').value})});state.editor=null;$('editor').close();state.autoMissing=true;notice('Исправление сохранено. Остальные готовые главы сохраняются. Нажми «Озвучить выделенные», чтобы продолжить.');}finally{$('editor-save').disabled=false;}});});
  poll();
})();
