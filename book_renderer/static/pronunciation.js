(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const dialog=$('pronunciation');
  let editor=null,previewSequence=0,previewTimer=null;
  const clone=value=>JSON.parse(JSON.stringify(value));
  const key=value=>value.normalize('NFC').trim().replace(/\s+/g,' ').toLocaleLowerCase('ru-RU');
  const accentLabel=value=>value.replace(/\+([аеёиоуыэюя])/gi,(_,vowel)=>vowel.toUpperCase());
  function message(text,error=false){$('pronunciation-message').textContent=text;$('pronunciation-message').classList.toggle('danger',error);}
  function busy(value){dialog.querySelectorAll('button,input,select,textarea').forEach(element=>{element.disabled=value;});}
  function resetReplacement(){editor.replacementIndex=null;$('replacement-form').reset();$('replacement-add').textContent='Добавить замену';$('replacement-cancel').hidden=true;}
  function resetStress(){editor.stressIndex=null;$('stress-form').reset();$('stress-add').textContent='Добавить ударение';$('stress-cancel').hidden=true;}
  function row(text,onEdit,onDelete){
    const item=document.createElement('div');item.className='pronunciation-rule';
    const label=document.createElement('p');label.textContent=text;item.append(label);
    const actions=document.createElement('div');actions.className='actions';
    for(const [title,handler] of [['Изменить',onEdit],['Удалить',onDelete]]){
      const button=document.createElement('button');button.type='button';button.className=title==='Удалить'?'quiet danger':'quiet';button.textContent=title;button.title=title==='Удалить'?'Удалить правило из списка; затем сохрани изменения.':'Открыть правило для изменения.';button.addEventListener('click',handler);actions.append(button);
    }
    item.append(actions);return item;
  }
  function renderRules(){
    const rules=editor.rules;$('replacement-count').textContent=rules.replacements.length;$('stress-count').textContent=rules.stresses.length;
    $('replacement-list').replaceChildren(...rules.replacements.map((rule,index)=>row(`${rule.from} → ${rule.to||'(убрать из речи)'}`,()=>{
      editor.replacementIndex=index;$('replacement-from').value=rule.from;$('replacement-to').value=rule.to;$('replacement-add').textContent='Сохранить замену';$('replacement-cancel').hidden=false;$('replacement-from').focus();
    },()=>{editor.rules.replacements.splice(index,1);resetReplacement();changed();})));
    $('stress-list').replaceChildren(...rules.stresses.map((word,index)=>row(accentLabel(word),()=>{
      editor.stressIndex=index;$('stress-word').value=accentLabel(word);$('stress-add').textContent='Сохранить ударение';$('stress-cancel').hidden=false;$('stress-word').focus();
    },()=>{editor.rules.stresses.splice(index,1);resetStress();changed();})));
    for(const [id,empty] of [['replacement-list',!rules.replacements.length],['stress-list',!rules.stresses.length]]){
      if(empty){const text=document.createElement('p');text.className='muted small';text.textContent='Пока нет правил.';$(id).append(text);}
    }
  }
  function changed(){renderRules();message('Есть несохранённые изменения.');schedulePreview();}
  function addReplacement(){
    const source=$('replacement-from').value.trim().replace(/\s+/g,' '),target=$('replacement-to').value.trim().replace(/\s+/g,' '),index=editor.replacementIndex;
    if(!source){message('Укажи, что заменить.',true);$('replacement-from').focus();return false;}
    if(editor.rules.replacements.some((rule,position)=>position!==index&&key(rule.from)===key(source))){message('Такая замена уже есть. Измени существующую строку.',true);return false;}
    if(index===null&&editor.rules.replacements.length>=300){message('Можно сохранить до 300 замен.',true);return false;}
    const rule={from:source,to:target};if(index===null)editor.rules.replacements.push(rule);else editor.rules.replacements[index]=rule;
    resetReplacement();changed();return true;
  }
  function markedWord(raw){
    const value=raw.normalize('NFC').trim(),letters=[],positions=new Set(),vowels='аеёиоуыэюя',explicit=value.includes('+')||value.includes('\u0301');
    if(!/^[а-яёА-ЯЁ+\u0301]{1,80}$/.test(value))throw Error('Укажи одно русское слово: например, злодЕй.');
    for(let index=0;index<value.length;index++){
      const ch=value[index],lower=ch.toLowerCase();
      if(ch==='+'){if(!vowels.includes((value[index+1]||'').toLowerCase())||!value[index+1])throw Error('Поставь + перед гласной.');positions.add(letters.length);}
      else if(ch==='\u0301'){if(!letters.length||!vowels.includes(letters[letters.length-1]))throw Error('Поставь знак ударения после гласной.');positions.add(letters.length-1);}
      else{if(!explicit&&ch!==lower&&vowels.includes(lower))positions.add(letters.length);letters.push(lower);}
    }
    if(positions.size!==1)throw Error('Отметь ровно одну ударную гласную: злодЕй.');
    const position=[...positions][0],word=letters.join('');return word.slice(0,position)+'+'+word.slice(position);
  }
  function addStress(){
    let word;try{word=markedWord($('stress-word').value);}catch(error){message(error.message,true);return false;}
    const index=editor.stressIndex;
    if(editor.rules.stresses.some((value,position)=>position!==index&&value.replace('+','')===word.replace('+',''))){message('Ударение этого слова уже задано. Измени существующую строку.',true);return false;}
    if(index===null&&editor.rules.stresses.length>=300){message('Можно сохранить до 300 ударений.',true);return false;}
    if(index===null)editor.rules.stresses.push(word);else editor.rules.stresses[index]=word;
    resetStress();changed();return true;
  }
  function schedulePreview(){
    previewSequence++;clearTimeout(previewTimer);$('pronunciation-stats').textContent='Предпросмотр обновляется…';
    previewTimer=setTimeout(()=>preview(),600);
  }
  async function preview(){
    clearTimeout(previewTimer);if(!editor||!editor.chapter||editor.saving)return;
    const current=editor,sequence=++previewSequence;
    try{
      const data=await current.api(`books/${current.bookId}/pronunciation/preview`,{method:'POST',body:JSON.stringify({document_revision:current.revision,chapter:current.chapter,text:$('pronunciation-source').value,rules:current.rules})});
      if(editor!==current||sequence!==previewSequence)return;
      $('pronunciation-spoken').value=data.spoken;
      const sum=values=>values.reduce((total,value)=>total+value,0);
      $('pronunciation-stats').textContent=`В этой главе: замен ${sum(data.stats.replacements)}, ударений ${sum(data.stats.stresses)}. Для озвучки: ${data.spoken.length.toLocaleString('ru-RU')} знаков.`;
    }catch(error){if(editor===current&&sequence===previewSequence){$('pronunciation-spoken').value='';$('pronunciation-stats').textContent='Предпросмотр недоступен.';message(error.message,true);}}
  }
  async function selectChapter(index){
    const current=editor;if(!current||current.saving)return;
    if(current.chapter&&$('pronunciation-source').value!==current.source&&!confirm('Перейти без сохранения правок этой главы? Правила замен и ударений останутся в окне.')){$('pronunciation-chapter').value=String(current.chapter);return false;}
    previewSequence++;clearTimeout(previewTimer);busy(true);
    try{
      const data=await current.api(`books/${current.bookId}/chapters/${index}`);
      if(editor!==current)return;
      if(data.document_revision!==current.revision)throw Error('Текст книги изменился. Закрой окно и открой его заново.');
      current.chapter=index;current.source=data.chapter.text;$('pronunciation-chapter').value=String(index);$('pronunciation-source').value=current.source;$('pronunciation-spoken').value='';
      await preview();
      return true;
    }catch(error){if(editor===current){$('pronunciation-chapter').value=String(current.chapter||index);message(error.message,true);}return false;}
    finally{if(editor===current)busy(false);}
  }
  function close(){
    if(!editor||editor.saving)return;
    const dirty=JSON.stringify(editor.rules)!==editor.savedRules||$('pronunciation-source').value!==editor.source||$('replacement-from').value||$('replacement-to').value||$('stress-word').value;
    if(dirty&&!confirm('Закрыть без сохранения правил и правок текста?'))return;
    previewSequence++;clearTimeout(previewTimer);editor=null;dialog.close();
  }
  async function save(){
    if(!editor||editor.saving||!editor.chapter)return;
    if(($('replacement-from').value||$('replacement-to').value)&&!addReplacement())return;
    if($('stress-word').value&&!addStress())return;
    const current=editor,chapters=[...$('pronunciation-target-list').querySelectorAll('input:checked')].map(input=>Number(input.value));
    if(!chapters.length){message('Выбери хотя бы одну главу для применения правил.',true);return;}
    const payload={document_revision:current.revision,rules:current.rules,chapters};
    if($('pronunciation-source').value!==current.source)payload.chapter_edit={index:current.chapter,text:$('pronunciation-source').value};
    current.saving=true;previewSequence++;clearTimeout(previewTimer);busy(true);message('Применяем правила к тексту выбранных глав…');
    try{
      const result=await current.api(`books/${current.bookId}/pronunciation`,{method:'POST',body:JSON.stringify(payload)});
      if(editor!==current)return;
      current.revision=result.document_revision;current.rules=clone(result.rules);current.savedRules=JSON.stringify(current.rules);
      const chapter=await current.api(`books/${current.bookId}/chapters/${current.chapter}`);
      current.source=chapter.chapter.text;$('pronunciation-source').value=current.source;
      current.changes=result.changes;current.changesShown=0;
      const replacements=result.changes.filter(change=>change.kind==='replacement').length,stresses=result.changes.length-replacements;
      $('pronunciation-changes-open').hidden=!result.changes.length;
      $('pronunciation-changes-open').textContent=`Посмотреть изменения: ${result.changes.length}`;
      renderRules();message(`Применено: замен ${replacements}, ударений ${stresses}. Изменилось чтение глав: ${result.affected_chapters.length}. Для озвучки выбери неозвученные главы.`);
      await current.onSaved(result);
    }catch(error){if(editor===current)message(error.message,true);}
    finally{if(editor===current){current.saving=false;busy(false);await preview();}}
  }
  async function saveChapter(){
    if(!editor||editor.saving||!editor.chapter)return;
    const current=editor,chapter=current.chapters.find(item=>item.index===current.chapter);
    current.saving=true;previewSequence++;clearTimeout(previewTimer);busy(true);message('Сохраняем правку главы…');
    try{
      const result=await current.api(`books/${current.bookId}/chapter`,{method:'POST',body:JSON.stringify({document_revision:current.revision,index:current.chapter,title:chapter.title,text:$('pronunciation-source').value})});
      current.revision=result.document_revision;
      const saved=await current.api(`books/${current.bookId}/chapters/${current.chapter}`);
      const before=Array.from(current.source),after=Array.from(saved.chapter.text);let prefix=0,suffix=0;
      while(prefix<before.length&&prefix<after.length&&before[prefix]===after[prefix])prefix++;
      while(suffix<before.length-prefix&&suffix<after.length-prefix&&before[before.length-1-suffix]===after[after.length-1-suffix])suffix++;
      const delta=after.length-before.length;
      for(const change of current.changes.filter(item=>item.chapter===current.chapter)){
        if(change.end<=prefix)continue;
        if(change.start>=before.length-suffix){change.start+=delta;change.end+=delta;}
        else{change.stale=true;change.start=prefix;change.end=prefix;}
      }
      current.source=saved.chapter.text;$('pronunciation-source').value=current.source;
      message('Правка главы сохранена. Для нового аудио переозвучь изменённую главу.');await current.onSaved(result);
    }catch(error){if(editor===current)message(error.message,true);}
    finally{if(editor===current){current.saving=false;busy(false);await preview();}}
  }
  function targetCount(){const items=[...$('pronunciation-target-list').querySelectorAll('input')];$('pronunciation-target-count').textContent=`· выбрано ${items.filter(item=>item.checked).length} из ${items.length}`;}
  function showChanges(reset=false){
    if(!editor)return;
    const list=$('pronunciation-changes-list');if(reset){editor.changesShown=0;list.replaceChildren();}
    const end=Math.min(editor.changesShown+100,editor.changes.length);
    for(const change of editor.changes.slice(editor.changesShown,end)){
      const button=document.createElement('button');button.type='button';button.className='pronunciation-change';
      const heading=document.createElement('strong');heading.textContent=`${change.chapter}. ${change.chapter_title} · ${change.kind==='stress'?'Ударение':'Замена'}: ${change.before} → ${change.after||'∅'}${change.stale?' · место исправлено вручную':''}`;
      const context=document.createElement('span');context.textContent=change.context;button.append(heading,context);
      button.title='Открыть главу и перейти к этому месту.';
      button.addEventListener('click',async()=>{
        if(!editor)return;
        const current=editor;if(!await selectChapter(change.chapter)||editor!==current)return;
        const input=$('pronunciation-source'),points=Array.from(input.value);
        let start=points.slice(0,change.start).join('').length,end=points.slice(0,change.end).join('').length;
        if(change.stale||input.value.slice(start,end)!==change.actual){end=start;message('Текст после применения изменился: проверь место рядом с курсором.',true);}
        $('pronunciation-changes').close();input.focus();input.setSelectionRange(start,end);input.scrollTop=input.scrollHeight*start/Math.max(input.value.length,1);
      });list.append(button);
    }
    editor.changesShown=end;$('pronunciation-changes-more').hidden=end>=editor.changes.length;
  }
  $('replacement-form').addEventListener('submit',event=>{event.preventDefault();if(editor&&!editor.saving)addReplacement();});
  $('stress-form').addEventListener('submit',event=>{event.preventDefault();if(editor&&!editor.saving)addStress();});
  $('replacement-cancel').addEventListener('click',resetReplacement);$('stress-cancel').addEventListener('click',resetStress);
  $('pronunciation-chapter').addEventListener('change',()=>selectChapter(Number($('pronunciation-chapter').value)));
  $('pronunciation-source').addEventListener('input',()=>{message('Есть несохранённые правки текста.');schedulePreview();});
  $('pronunciation-preview').addEventListener('click',preview);$('pronunciation-save').addEventListener('click',save);$('pronunciation-close').addEventListener('click',close);
  $('pronunciation-save-chapter').addEventListener('click',saveChapter);
  $('pronunciation-target-list').addEventListener('change',targetCount);
  for(const [id,checked] of [['pronunciation-target-all',true],['pronunciation-target-none',false]])$(id).addEventListener('click',()=>{$('pronunciation-target-list').querySelectorAll('input').forEach(input=>{input.checked=checked;});targetCount();});
  $('pronunciation-changes-open').addEventListener('click',()=>{showChanges(true);$('pronunciation-changes').showModal();});
  $('pronunciation-changes-close').addEventListener('click',()=>$('pronunciation-changes').close());
  $('pronunciation-changes-more').addEventListener('click',()=>showChanges());
  dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  for(const [button,input] of [['replacement-selection','replacement-from'],['stress-selection','stress-word']]){
    $(button).addEventListener('click',()=>{const text=$('pronunciation-source'),selected=text.value.slice(text.selectionStart,text.selectionEnd).trim();if(!selected){message('Сначала выдели слово или фразу в левом поле.',true);return;}$(input).value=selected;$(input).focus();});
  }
  window.ZvukbookPronunciation={open:async(book,api,onSaved)=>{
    if(book.status!=='ready'||!book.chapter_list.length)throw Error('Сначала дождись готового текста книги.');
    const rules=clone(book.pronunciation||{replacements:[],stresses:[]});
    editor={bookId:book.id,revision:book.document_revision,rules,savedRules:JSON.stringify(rules),source:'',chapter:0,chapters:book.chapter_list,changes:[],changesShown:0,saving:false,replacementIndex:null,stressIndex:null,api,onSaved};
    resetReplacement();resetStress();renderRules();message('');$('pronunciation-book').textContent=book.title;$('pronunciation-source').value='';$('pronunciation-spoken').value='';$('pronunciation-stats').textContent='';
    $('pronunciation-chapter').replaceChildren(...book.chapter_list.map(chapter=>{const option=document.createElement('option');option.value=chapter.index;option.textContent=`${chapter.index}. ${chapter.title}`;return option;}));
    $('pronunciation-target-list').replaceChildren(...book.chapter_list.map(chapter=>{const label=document.createElement('label'),input=document.createElement('input'),text=document.createElement('span');input.type='checkbox';input.value=chapter.index;input.checked=true;text.textContent=`${chapter.index}. ${chapter.title}`;label.append(input,text);return label;}));
    targetCount();$('pronunciation-changes-open').hidden=true;$('pronunciation-legacy').hidden=!book.legacy_pronunciation;
    dialog.showModal();await selectChapter(book.chapter_list[0].index);
  }};
})();
