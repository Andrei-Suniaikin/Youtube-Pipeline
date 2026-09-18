"""Execute the actual menu JS, preserving JavaScript callback argument semantics."""
import ast
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node') or '/Users/vova/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'


def switch_script():
    tree=ast.parse((ROOT/'src/yt_studio.py').read_text())
    function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='open_account_menu')
    return next(node.args[1].value for node in ast.walk(function)
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='_js'
                and isinstance(node.args[1],ast.Constant) and 'var sw =' in node.args[1].value)


class AccountMenuTest(unittest.TestCase):
    def execute(self, items):
        code='''const items=ITEMS, clicked=[];
        const run=new Function('deepAll','realClick','document', 'return function(){'+SOURCE+'}');
        const found=run(()=>items,e=>clicked.push(e.key),{})('ytd-compact-link-renderer','SWITCH_ACCOUNTS');
        console.log(JSON.stringify({found,clicked}));'''
        code=code.replace('ITEMS',json.dumps(items)).replace('SOURCE',json.dumps(switch_script()))
        return json.loads(subprocess.run([NODE,'-e',code],capture_output=True,text=True,check=True,timeout=5).stdout)

    def test_native_callback_uses_outer_selector_parameters(self):
        items=[{'tagName':'YTD-COMPACT-LINK-RENDERER','key':'help','data':{'icon':{'iconType':'HELP'}}},
               {'tagName':'YTD-COMPACT-LINK-RENDERER','key':'switch','data':{'icon':{'iconType':'SWITCH_ACCOUNTS'}}}]
        self.assertEqual(self.execute(items),{'found':True,'clicked':['switch']})

    def test_missing_or_wrong_machine_item_does_not_click(self):
        for items in ([],[{'tagName':'YTD-COMPACT-LINK-RENDERER','key':'empty'}],
                      [{'tagName':'DIV','key':'wrong','data':{'icon':{'iconType':'SWITCH_ACCOUNTS'}}}]):
            self.assertEqual(self.execute(items),{'found':False,'clicked':[]})


if __name__=='__main__':unittest.main()
