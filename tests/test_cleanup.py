import csv, importlib.util, os, tempfile, time, unittest, sys
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('navidrome_cleanup', str(Path(__file__).resolve().parents[1] / 'navidrome_to_qbittorrent_quarantine.py'))
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
class FakeQb:
    torrents=[]
    file_lists={}
    status_calls=[]
    def __init__(self, *a): pass
    def request(self, endpoint, params=None, json_result=True):
        if endpoint=='torrents/info':
            return [dict(t) for t in self.torrents if 'hashes' not in (params or {}) or t['hash']==params['hashes']]
        if endpoint=='torrents/files':
            v=self.file_lists[params['hash']]
            if isinstance(v,Exception): raise v
            return [dict(f) for f in v]
        if endpoint=='torrents/filePrio':
            self.status_calls.append(params)
            for f in self.file_lists[params['hash']]:
                if int(f['index']) in [int(z) for z in params['id'].split('|')]:f['priority']=0
            return ''
        raise RuntimeError(endpoint)
class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.root=root
        self.music=root/'Music'; self.music.mkdir()
        self.quarantine=root/'.MusicQuarantine'
        for sub, name, content in [('Known','1.flac',b'known file'),('Random','bad.flac',b'bad song!!'),('Anthem of the Heart','ambiguous.flac',b'unreadabl!')]:
            d=self.music/sub;d.mkdir();p=d/name;p.write_bytes(content)
            old=time.time()-10000;os.utime(p,(old,old))
        self.songs=[{'id':'1','path':'/music/Known/1.flac','title':'known','size':10},
            {'id':'2','path':'/music/Random/bad.flac','title':'bad','size':10},
            {'id':'3','path':'/music/Anthem of the Heart/ambiguous.flac','title':'unknown','size':10}]
        self.known={'hash':'A'*40,'save_path':'/DATA/Media/Music/Known','name':'Known','category':'Known','state':'uploading'}
        self.unknown={'hash':'B'*40,'save_path':'/DATA/Media/Music/Anthem of the Heart','name':'Unknown','category':'Anthem','state':'forcedMetaDL'}
        FakeQb.torrents=[self.known,self.unknown]
        FakeQb.file_lists={self.known['hash']:[{'name':'1.flac','size':10,'index':0,'priority':1},{'name':'other.flac','size':7,'index':1,'priority':1}],self.unknown['hash']:[]}
        FakeQb.status_calls=[]
        self.args=['script.py','--nav-url','http://127.0.0.1:4533','--qb-url','http://127.0.0.1:8181','--nav-root','/music','--qb-root','/DATA/Media/Music','--host-music-root',str(self.music),'--quarantine-root',str(self.quarantine),'--report',str(root/'report.csv')]
    def run_app(self, opts, confirm=()):
        with patch.object(m,'QbClient',FakeQb),patch.object(m,'subsonic_call', return_value={'starred2':{'song':self.songs}}),patch.dict(os.environ,{'NAVIDROME_PASSWORD':'x','QBITTORRENT_PASSWORD':'y'}),patch.object(sys,'argv',self.args+opts),patch('builtins.input',side_effect=['n','q',*confirm]):
            return m.main()
    def rows(self):
        with (self.root/'report.csv').open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))
    def test_strict_dry_run_blocks(self):
        with self.assertRaisesRegex(RuntimeError,'Quarantine safely blocked'):
            self.run_app([])
    def test_ignore_dry_run_previews(self):
        self.assertEqual(self.run_app(['--ignore-missing-metadata']),0)
        self.assertEqual([r['status'] for r in self.rows()],['READY','MOVE_READY','MOVE_READY'])
        self.assertTrue((self.music/'Random'/'bad.flac').exists())
    def test_ignore_apply_and_move(self):
        self.assertEqual(self.run_app(['--ignore-missing-metadata','--apply','--move-unmatched'],['APPLY','MOVE']),0)
        self.assertEqual([r['status'] for r in self.rows()],['APPLIED','MOVED','MOVED'])
        self.assertEqual(FakeQb.file_lists[self.known['hash']][0]['priority'],0)
        self.assertEqual((self.quarantine/'Random'/'bad.flac').read_bytes(),b'bad song!!')
        self.assertEqual((self.quarantine/'Anthem of the Heart'/'ambiguous.flac').read_bytes(),b'unreadabl!')
        self.assertFalse((self.music/'Random'/'bad.flac').exists())
        self.assertTrue((self.music/'Known'/'1.flac').exists())
    def test_metadata_arrives_before_move(self):
        original_refresh=m.refresh_torrent_index
        def refresh_then_return(qb,*args,**kwargs):
            FakeQb.file_lists[self.unknown['hash']]=[{'name':'ambiguous.flac','size':10,'index':0,'priority':1}]
            return original_refresh(qb,*args,**kwargs)
        with patch.object(m,'refresh_torrent_index',refresh_then_return):
            self.assertEqual(self.run_app(['--ignore-missing-metadata','--move-unmatched'],['MOVE']),0)
        self.assertEqual([r['status'] for r in self.rows()],['READY','MOVED','SKIP_CHANGED'])
        self.assertTrue((self.music/'Anthem of the Heart'/'ambiguous.flac').exists())
    def test_zero_hearted_qb_matches_still_quarantines(self):
        # User's real case: formerly matched tracks have already been moved out
        # of Navidrome; only songs with no currently known torrent remain.
        self.songs = self.songs[1:]
        self.assertEqual(self.run_app(['--ignore-missing-metadata']),0)
        self.assertEqual([r['status'] for r in self.rows()],['MOVE_READY','MOVE_READY'])
        self.assertEqual(self.run_app(['--ignore-missing-metadata','--apply','--move-unmatched'],['MOVE']),0)
        self.assertEqual([r['status'] for r in self.rows()],['MOVED','MOVED'])
        self.assertTrue((self.quarantine/'Random'/'bad.flac').exists())
        self.assertFalse((self.music/'Random'/'bad.flac').exists())
        self.assertEqual(FakeQb.status_calls,[])

    def test_wrong_qb_root_blocks_quarantine(self):
        self.args[self.args.index('--qb-root')+1] = '/WRONG'
        with self.assertRaisesRegex(RuntimeError, 'No qBittorrent file paths fall under'):
            self.run_app(['--ignore-missing-metadata'])

    def test_wrong_nav_root_blocks_quarantine(self):
        self.args[self.args.index('--nav-root')+1] = '/WRONG'
        with self.assertRaisesRegex(RuntimeError, 'None of the Navidrome starred paths'):
            self.run_app(['--ignore-missing-metadata'])

    def test_zero_hearted_matches_and_bad_file_size_does_not_move(self):
        self.songs = [dict(self.songs[1],size=999)]
        self.assertEqual(self.run_app(['--ignore-missing-metadata','--move-unmatched'],['MOVE']),0)
        self.assertEqual([r['status'] for r in self.rows()],['SKIP_DISK_SIZE'])
        self.assertTrue((self.music/'Random'/'bad.flac').exists())

    def test_other_api_failure_does_not_get_ignored(self):
        from urllib.error import URLError
        FakeQb.file_lists[self.unknown['hash']]=URLError('simulated temporary failure')
        with self.assertRaisesRegex(RuntimeError,'Quarantine safely blocked'):
            self.run_app(['--ignore-missing-metadata'])
    def test_csv_formula_injection_safe(self):
        self.assertEqual(m.csv_safe('=HYPERLINK("oops")'), "'=HYPERLINK(\"oops\")")
        self.assertEqual(m.csv_safe('normal title'), 'normal title')
        self.assertEqual(m.csv_safe('  +SUM(1,2)'), "'  +SUM(1,2)")

    def test_reject_relative_paths_and_traversal(self):
        self.assertIsNone(m.relative_path('Album/track.flac', '/music'))
        self.assertIsNone(m.relative_path('/music/../other.flac', '/music'))
        self.assertIsNone(m.qb_path('/DATA/Media/Music', '../outside.flac'))
        self.assertIsNone(m.qb_path('/DATA/Media/Music', '/etc/passwd'))

    def test_whole_torrent_protected_at_apply_time(self):
        # While applying, another user deselected the only other selected file.
        # Recheck should refuse to turn off the last selected file.
        original_request = FakeQb.request
        def changed_selection(qb, endpoint, params=None, json_result=True):
            if endpoint == 'torrents/info' and 'hashes' in (params or {}):
                FakeQb.file_lists[self.known['hash']][1]['priority'] = 0
            return original_request(qb, endpoint, params, json_result)
        with patch.object(FakeQb, 'request', changed_selection):
            self.assertEqual(self.run_app(['--ignore-missing-metadata', '--apply'], ['APPLY']), 0)
        self.assertEqual(self.rows()[0]['status'], 'WHOLE_TORRENT')
        self.assertEqual(FakeQb.file_lists[self.known['hash']][0]['priority'], 1)
        self.assertEqual(FakeQb.status_calls, [])

    def test_reject_symlink_candidate(self):
        (self.music/'Random'/'bad.flac').unlink()
        outside=self.root/'outside.flac'; outside.write_bytes(b'bad song!!')
        (self.music/'Random'/'bad.flac').symlink_to(outside)
        self.assertEqual(self.run_app(['--ignore-missing-metadata']),0)
        self.assertEqual(self.rows()[1]['status'], 'SKIP_MISSING')

    def test_applied_priority_does_not_remove_source(self):
        self.assertEqual(self.run_app(['--ignore-missing-metadata','--apply'],['APPLY']),0)
        self.assertTrue((self.music/'Known'/'1.flac').exists())
        self.assertEqual(self.rows()[0]['status'],'APPLIED')

if __name__=='__main__':unittest.main(verbosity=2)

class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.music = self.root / 'Music'
        self.music.mkdir()
        self.path = self.root / 'settings' / 'config.json'

    def test_config_saves_without_passwords(self):
        config = {'nav_url':'http://127.0.0.1:4533','qb_url':'http://127.0.0.1:8181',
                  'nav_root':'/music','qb_root':'/DATA/Media/Music',
                  'host_music_root':str(self.music),
                  'quarantine_root':str(self.root/'.MusicQuarantine'),
                  'nav_user':'asuka','qb_user':'admin',
                  'ignore_missing_metadata':True, 'protect_blocked_folders':False}
        m.save_config(self.path, config)
        self.assertEqual(m.load_config(self.path), config)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('password', self.path.read_text().lower())

    def test_docker_autodetect_parent_mount(self):
        import json
        items=[{'Name':'/navidrome','Config':{'Image':'deluan/navidrome:latest'},
                'Mounts':[{'Type':'bind','Source':str(self.music),'Destination':'/music'},
                          {'Type':'bind','Source':'/someapp','Destination':'/data'}],
                'NetworkSettings':{'Ports':{'4533/tcp':[{'HostPort':'4533'}]}}},
               {'Name':'/qbittorrent','Config':{'Image':'ghcr.io/hotio/qbittorrent'},
                'Mounts':[{'Type':'bind','Source':str(self.root),'Destination':'/DATA'}],
                'NetworkSettings':{'Ports':{'8080/tcp':[{'HostPort':'8181'}]}}}]
        from types import SimpleNamespace
        def fake_run(cmd, **kwargs):
            if cmd[1]=='ps': return SimpleNamespace(stdout='one\ntwo\n')
            return SimpleNamespace(stdout=json.dumps(items))
        with patch.object(m.subprocess,'run',side_effect=fake_run):
            found=m.docker_defaults()
        self.assertEqual(found['nav_root'],'/music')
        self.assertEqual(found['qb_root'],'/DATA/Music')
        self.assertEqual(found['qb_url'],'http://127.0.0.1:8181')
        self.assertEqual(found['host_music_root'],str(self.music))

    def test_setup_wizard_accepts_detected_defaults(self):
        fake={"nav_url":"http://127.0.0.1:4533", "qb_url":"http://127.0.0.1:8181",
              "nav_root":"/music", "qb_root":"/DATA/Media/Music",
              "host_music_root":str(self.music),
              "quarantine_root":str(self.root/'.MusicQuarantine')}
        with patch.object(m,'docker_defaults',return_value=fake), \
             patch.object(sys.stdin,'isatty',return_value=True), \
             patch('builtins.input',side_effect=['']*6+['navuser','qbuser','y','SAVE']):
            self.assertEqual(m.interactive_setup(self.path),0)
        read=m.load_config(self.path)
        self.assertEqual(read['nav_url'],fake['nav_url'])
        self.assertEqual(read['ignore_missing_metadata'],True)
        self.assertEqual(read['nav_user'],'navuser')
        self.assertEqual(read['qb_user'],'qbuser')

    def test_malformed_config_rejected(self):
        self.path.parent.mkdir()
        self.path.write_text('{"ignore_missing_metadata": "yes"}')
        with self.assertRaisesRegex(RuntimeError,'Invalid boolean'):
            m.load_config(self.path)

    def test_clean_flag_uses_saved_config(self):
        # Test CLI compatibility without making actual API calls or file changes.
        config={'nav_url':'http://127.0.0.1:4533','qb_url':'http://127.0.0.1:8181',
                'nav_root':'/music','qb_root':'/DATA/Media/Music',
                'host_music_root':str(self.music), 'quarantine_root':str(self.root/'.MusicQuarantine'),
                'nav_user':'n','qb_user':'q','ignore_missing_metadata':True}
        m.save_config(self.path,config)
        class FakeQbEmpty:
            def __init__(self,*_):pass
            def request(self, endpoint, params=None, json_result=True):
                if endpoint=='torrents/info': return [{'hash':'a'*40,'save_path':'/DATA/Media/Music','name':'test'}]
                if endpoint=='torrents/files':return [{'name':'Album/whatever.flac','size':100,'index':0,'priority':1}]
                return ''
        # No songs. Verify the command runs through config without argument errors.
        with patch.object(m,'QbClient',FakeQbEmpty), \
             patch.object(m,'subsonic_call',return_value={'starred2':{'song':[]}}), \
             patch.dict(os.environ,{'NAVIDROME_PASSWORD':'x','QBITTORRENT_PASSWORD':'y'}), \
             patch.object(sys,'argv',['script.py','--config',str(self.path),
                                       '--report',str(self.root/'report.csv'), '--clean']):
            self.assertEqual(m.main(),0)
        self.assertTrue((self.root/'report.csv').exists())
