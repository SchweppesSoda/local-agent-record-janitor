"""Independent TEMP-only Chromium local-storage fixture and readback oracle."""
import json
from pathlib import Path
import subprocess

from local_agent_record_janitor import office_leveldb as level


def local_storage(root, server, selected, keep, *, inspect=False, attachment=False):
    node, dependency, helper = level.runtime()
    path = Path(root) / "Local Storage/leveldb"
    path.parent.mkdir(parents=True, exist_ok=True)
    request = {"path": str(path), "server": server, "selected": selected, "keep": keep,
               "inspect": inspect, "attachment": attachment}
    script = r'''
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const h=require(process.argv[1]),q=JSON.parse(fs.readFileSync(0,'utf8'));
const req=require('node:module').createRequire(path.join(process.env.LARJ_LEVELDB_RUNTIME,'package.json'));
const {ClassicLevel}=req('classic-level');
const prefix=Buffer.from('_paseo://app\0'),key=n=>Buffer.concat([prefix,h.encodeString(n)]);
const record=text=>({input:{text,attachments:[]},lifecycle:'active',updatedAt:1,version:1});
const drafts={version:5,state:{drafts:{
 [`agent:${q.server}:${q.selected}`]:record('PRIVATE_PASEO_ERASE'),
 [`agent:${q.server}:${q.keep}`]:record('KEEP_LOCAL'),
 [`agent:remote:${q.selected}`]:record('KEEP_REMOTE')},createModalDraft:record('KEEP_MODAL')}};
if(q.attachment) drafts.state.drafts[`agent:${q.server}:${q.selected}`].input.attachments=[{unknown:'external'}];
const tab=(tabId,agentId)=>({tabId,target:{kind:'agent',agentId},createdAt:1});
const layout={version:2,state:{layoutByWorkspace:{[`${q.server}:workspace`]:{
 root:{kind:'pane',pane:{id:'pane',tabIds:['erase','keep'],focusedTabId:'erase',tabs:[tab('erase',q.selected),tab('keep',q.keep)]}},focusedPaneId:'pane'}}}};
const db=new ClassicLevel(q.path,{keyEncoding:'buffer',valueEncoding:'buffer',createIfMissing:!q.inspect});
(async()=>{await db.open();if(!q.inspect){await db.batch([
 {type:'put',key:Buffer.from('VERSION'),value:Buffer.from('1')},
 {type:'put',key:key('paseo-drafts'),value:h.encodeString(JSON.stringify(drafts))},
 {type:'put',key:key('workspace-layout-state'),value:h.encodeString(JSON.stringify(layout))},
 {type:'put',key:key('opaque-settings'),value:Buffer.from('KEEP_OPAQUE')},
 {type:'put',key:Buffer.concat([Buffer.from('_https://other.example\0'),h.encodeString('foreign')]),value:Buffer.from('KEEP_FOREIGN')},
 {type:'put',key:Buffer.from('META:paseo://app'),value:Buffer.concat([Buffer.from([8]),h.varint(13000000000000000n),Buffer.from([16]),h.varint(100000n)])}
 ],{sync:true});await db.compactRange(Buffer.from(''),Buffer.from([255]),{keyEncoding:'buffer'});
}else{
 const actual=JSON.parse(h.decodeString(await db.get(key('paseo-drafts'))));
 delete drafts.state.drafts[`agent:${q.server}:${q.selected}`];assert.deepEqual(actual,drafts);
 const after=JSON.parse(h.decodeString(await db.get(key('workspace-layout-state'))));
 const pane=layout.state.layoutByWorkspace[`${q.server}:workspace`].root.pane;
 pane.tabIds=['keep'];pane.focusedTabId='keep';pane.tabs=[tab('keep',q.keep)];assert.deepEqual(after,layout);
 assert.equal((await db.get(key('opaque-settings'))).toString(),'KEEP_OPAQUE');
 assert.equal((await db.get(Buffer.concat([Buffer.from('_https://other.example\0'),h.encodeString('foreign')]))).toString(),'KEEP_FOREIGN');
}await db.close();console.log(JSON.stringify({verified:true}));})().catch(()=>process.exitCode=1);
'''
    result = subprocess.run([str(node), "-e", script, str(helper)], input=json.dumps(request).encode(),
        capture_output=True, cwd=dependency, env=level._environment(dependency), timeout=30)
    if result.returncode or json.loads(result.stdout).get("verified") is not True:
        raise AssertionError("Synthetic Paseo local-storage seed/readback failed")
