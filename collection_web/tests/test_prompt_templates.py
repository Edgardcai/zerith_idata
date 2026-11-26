import json
from pathlib import Path
import shutil
import subprocess
import unittest

from tasks import compose_task, parse_targets, validate_task


class PromptTemplateTests(unittest.TestCase):
    def test_batch4_names_match_operator_list_and_all_modes(self):
        root=Path(__file__).resolve().parents[1]
        expected=json.loads((Path(__file__).parent/'fixtures/catalog_batch4.json').read_text())
        source=(root/'static/app.js').read_text()
        products=json.loads(source.split('const PRODUCTS = Object.freeze(',1)[1].split(');',1)[0])
        self.assertEqual(len(expected),33)
        self.assertNotIn('Monster Pink',products)
        for name,label in expected.items():
            self.assertEqual(products[name],label,name)
            for mode in ('shelf_grasp','tabletop','shelf_place','fridge_grasp'):
                for hand in ('left','right') if mode=='fridge_grasp' else ('left','right','both'):
                    with self.subTest(product=name,mode=mode,hand=hand):
                        cfg,targets=validate_task({'prompt_mode':mode,'hand_mode':hand,'left':name,'right':name})
                        parsed_targets = {'left': name, 'right': ''} if mode=='fridge_grasp' else {k:targets[k] for k in ('left','right')}
                        self.assertEqual(parse_targets(cfg['task_name']), parsed_targets)
                        self.assertIn(name,cfg['task_name'])
                        self.assertTrue(cfg['task_name'].isascii())
    def test_new_submissions_reject_retired_monster_pink_including_old_pages(self):
        for mode in ('shelf_grasp','tabletop','shelf_place','fridge_grasp'):
            for hand in ('left','right') if mode=='fridge_grasp' else ('left','right','both'):
                with self.subTest(mode=mode,hand=hand),self.assertRaisesRegex(ValueError,'Monster Red'):
                    validate_task({'prompt_mode':mode,'hand_mode':hand,'left':'Monster Pink','right':'Monster Pink'})
        for payload in [
            {'task_name':'Grasp Monster Pink with the left hand.'},
            {'task_name':'Grasp Monster Pink with the left hand.','left':'Monster Red'},
            {'task_name':'Custom task','right':' monster pink '},
        ]:
            with self.subTest(payload=payload),self.assertRaisesRegex(ValueError,'Monster Red'):
                validate_task(payload)
    def test_exact_prompts_and_stage_counts(self):
        expected={
            ('shelf_grasp','left'):('Grasp Tea with the left hand.',1),
            ('shelf_grasp','right'):('Grasp Coffee with the right hand.',1),
            ('shelf_grasp','both'):('Grasp Tea with the left hand and then grasp Coffee with the right hand.',2),
            ('tabletop','left'):('Place the item held in the left hand on the table. Grasp Tea from the table with the left hand.',2),
            ('tabletop','right'):('Place the item held in the right hand on the table. Grasp Coffee from the table with the right hand.',2),
            ('tabletop','both'):('Place the item held in the left hand on the table, then place the item held in the right hand on the table. Grasp Tea from the table with the left hand, then grasp Coffee with the right hand.',4),
            ('shelf_place','left'):('Place Tea on the shelf with the left hand.',1),
            ('shelf_place','right'):('Place Coffee on the shelf with the right hand.',1),
            ('shelf_place','both'):('Place Tea on the shelf with the left hand, then place Coffee with the right hand.',2),
        }
        for hand, product in [('left', 'Tea'), ('right', 'Coffee')]:
            expected['fridge_grasp', hand] = (f'Open the warming cabinet door and grasp {product}', 1)
        for (mode,hand),(prompt,total) in expected.items():
            with self.subTest(mode=mode,hand=hand):
                config,targets=validate_task({'prompt_mode':mode,'hand_mode':hand,'left':'Tea','right':'Coffee'})
                self.assertEqual(config['task_name'],prompt);self.assertEqual(config['subtask_num'],total)
                self.assertEqual(len(targets['stage_prompts']),total);self.assertTrue(prompt.isascii())
                self.assertEqual(config['step_list'],[])
                for key in ('prompt_mode','hand_mode','stage_prompts'):self.assertNotIn(key,config)
                self.assertIn('Stage '+str(total)+':',config['action_desc'])
                if mode!='fridge_grasp':
                    self.assertEqual(parse_targets(prompt),{k:targets[k] for k in ('left','right')})
    def test_fridge_capitalizes_display_names_and_keeps_catalog_targets(self):
        config, targets = validate_task({'prompt_mode':'fridge_grasp','left':'tea','right':'If coconut'})
        self.assertEqual(targets['hand_mode'], 'left')
        self.assertEqual(targets['right'], '')
        self.assertEqual(config['task_name'], 'Open the warming cabinet door and grasp Tea')
        right_config, right_targets = validate_task({'prompt_mode':'fridge_grasp','hand_mode':'right','right':'tea'})
        self.assertEqual(right_config['task_name'], config['task_name'])
        self.assertEqual((right_targets['left'], right_targets['right']), ('', 'tea'))
        with self.assertRaises(ValueError):
            validate_task({'prompt_mode':'fridge_grasp','hand_mode':'both','left':'Tea','right':'If coconut'})
        self.assertEqual(config['subtask_num'], 1)
        self.assertEqual(targets['left'], 'tea')
        self.assertEqual(targets['stage_prompts'], [
            'Open the warming cabinet door and grasp Tea',
        ])
    def test_tabletop_order_is_left_place_right_place_left_grasp_right_grasp(self):
        stages=compose_task('tabletop','both','Tea','Coffee')['stage_prompts']
        self.assertEqual(stages,[
            'Place the item held in the left hand on the table.',
            'Place the item held in the right hand on the table.',
            'Grasp Tea from the table with the left hand.',
            'Grasp Coffee from the table with the right hand.'])
    def test_rejects_unknown_modes_empty_or_non_english_products_and_mismatches(self):
        base={'prompt_mode':'tabletop','hand_mode':'both','left':'Tea','right':'Coffee'}
        for change in ({'prompt_mode':'other'},{'hand_mode':'none'},{'left':''},{'right':'商品'},
                       {'left':'../tea'},{'left':None},{'subtask_num':2},{'subtask_num':True},
                       {'task_name':'Grasp Tea with the left hand.'}):
            with self.subTest(change=change),self.assertRaises(ValueError):validate_task({**base,**change})
    def test_unused_hand_is_empty_and_legacy_manual_tasks_are_preserved(self):
        config,targets=validate_task({'prompt_mode':'tabletop','hand_mode':'left','left':'Tea','right':'Coffee'})
        self.assertEqual(targets['right'],'');self.assertNotIn('right',config['task_name'])
        config,targets=validate_task({'task_name':'Historical custom task','subtask_num':7})
        self.assertEqual(config['task_name'],'Historical custom task');self.assertEqual(config['subtask_num'],7)
        self.assertNotIn('prompt_mode',targets)
    def test_parser_preserves_punctuation_and_removes_locations(self):
        for mode in ('shelf_grasp','tabletop','shelf_place','fridge_grasp'):
            hand='left' if mode=='fridge_grasp' else 'both'
            prompt=compose_task(mode,hand,'Dr. Pepper','M&M Red')['task_name']
            self.assertEqual(parse_targets(prompt),{'left':'Dr. Pepper','right':'M&M Red' if hand=='both' else ''})
        legacy='Open the warming cabinet door and grasp Tea (left) and Coffee (right). Open the warming cabinet door and place Tea (left) and Coffee (right).'
        self.assertEqual(parse_targets(legacy),{'left':'Tea','right':'Coffee'})
    def test_all_catalog_choices_fit_vendor_directory_limit(self):
        source=(Path(__file__).resolve().parents[1]/'static/app.js').read_text()
        products=json.loads(source.split('const PRODUCTS = Object.freeze(',1)[1].split(');',1)[0])
        longest=max(products,key=lambda x:len(x.encode()))
        for product in products:
            for mode in ('shelf_grasp','tabletop','shelf_place','fridge_grasp'):
                for hand in ('left','right') if mode=='fridge_grasp' else ('left','right','both'):
                    c,t=validate_task({'prompt_mode':mode,'hand_mode':hand,'left':product,'right':longest})
                    self.assertLessEqual(len(c['task_name'].encode()),230)
    def test_javascript_and_server_composition_match(self):
        if not shutil.which('node'):self.skipTest('Node unavailable; browser integration also covers parity')
        cases=[{'mode':m,'hand':h,'left':l,'right':r}
               for m in ('shelf_grasp','tabletop','shelf_place','fridge_grasp') for h in (('left','right') if m=='fridge_grasp' else ('left','right','both'))
               for l,r in [('Tea','Coffee'),('Dr. Pepper','M&M Red'),('Warheads Blue Raspberry','Warheads Blue Raspberry')]]
        script="require(process.argv[1]);let s='';process.stdin.on('data',x=>s+=x);process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(s).map(x=>TaskPrompts.compose(x.mode,x.hand,x.left,x.right)))));"
        path=Path(__file__).resolve().parents[1]/'static/task-prompts.js'
        result=subprocess.run(['node','-e',script,str(path)],input=json.dumps(cases),text=True,capture_output=True,check=True)
        for case,actual in zip(cases,json.loads(result.stdout)):
            self.assertEqual(actual,compose_task(case['mode'],case['hand'],case['left'],case['right']))
