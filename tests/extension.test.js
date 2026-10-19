const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function fixture() {
  const events = {};
  const nativeCalls = [];
  const chrome = {
    runtime: {id: 'a'.repeat(32), onInstalled:{addListener:fn=>events.installed=fn},
      onMessage:{addListener:fn=>events.message=fn},
      sendNativeMessage:async (...args)=>{nativeCalls.push(args);return {ok:true,id:'test'};}},
    contextMenus:{create:options=>events.menu=options,onClicked:{addListener:fn=>events.click=fn}},
    action:{setBadgeText:async()=>{},setBadgeBackgroundColor:async()=>{},setTitle:async()=>{}}
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../browser-extension/background.js'),'utf8'), {chrome,URL});
  return {events,nativeCalls,chrome};
}

test('context menu hands off only an explicit HTTP link', async()=>{
  const f=fixture();f.events.installed();assert.equal(f.events.menu.contexts[0],'link');
  await f.events.click({menuItemId:'flowget-link',linkUrl:'https://example.com/a.zip'});
  assert.equal(f.nativeCalls.length,1);assert.equal(f.nativeCalls[0][0],'com.flowget.bridge');
  assert.equal(f.nativeCalls[0][1].url,'https://example.com/a.zip');
});
test('non HTTP and embedded credentials never reach native messaging',async()=>{
  for(const url of ['file:///C:/x','javascript:alert(1)','https://u:p@example.com/x']){
    const f=fixture();await f.events.click({menuItemId:'flowget-link',linkUrl:url});assert.equal(f.nativeCalls.length,0);
  }
});
test('foreign extension messages do not trigger a download',()=>{
  const f=fixture();assert.equal(f.events.message({action:'add',url:'https://example.com/x'},{id:'other'},()=>{}),false);
  assert.equal(f.nativeCalls.length,0);
});
test('popup reports native rejection',async()=>{
  const f=fixture();f.chrome.runtime.sendNativeMessage=async()=>({ok:false,error:'unregistered'});
  const result=await new Promise(resolve=>f.events.message({action:'add',url:'https://example.com/x'},{id:'a'.repeat(32)},resolve));
  assert.equal(result.ok,false);assert.equal(result.error,'unregistered');
});
