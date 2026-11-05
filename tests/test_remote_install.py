import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import paramiko
from fastapi import HTTPException
from app import main, remote_install as remote

class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=main.DATA;main.DATA=Path(self.tmp.name)
        with main.conn() as c:
            c.execute("CREATE TABLE installs(id TEXT PRIMARY KEY,status TEXT,config TEXT)");remote.init(c)
        self.login=remote.SSHLogin(host='example.com',forward_id='1',fingerprint='SHA256:fixture',password='private-password')
    def tearDown(self):main.DATA=self.old;self.tmp.cleanup()
    def test_endpoint_requires_owned_ssh_mapping_and_global_destination(self):
        mapping={'id':'1','protocol':'tcp','internal_port':22,'public_port':30022}
        with patch.object(remote.vpsnat,'vm'),patch.object(remote.vpsnat,'forwards',return_value=[mapping]),patch.object(remote.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',30022))]):
            with self.assertRaises(HTTPException) as caught:remote.endpoint('123',self.login)
            self.assertEqual(caught.exception.status_code,422)
        with patch.object(remote.vpsnat,'vm'),patch.object(remote.vpsnat,'forwards',return_value=[mapping|{'internal_port':18080}]):
            with self.assertRaises(HTTPException):remote.endpoint('123',self.login)
        with patch.object(remote.vpsnat,'vm'),patch.object(remote.vpsnat,'forwards',return_value=[mapping]),patch.object(remote.socket,'getaddrinfo',return_value=[(2,1,6,'',('1.1.1.1',30022))]):
            self.assertEqual(remote.endpoint('123',self.login),('1.1.1.1',30022))
    def test_fingerprint_mismatch_sends_no_credentials(self):
        t=MagicMock();t.get_remote_server_key.return_value.asbytes.return_value=b'actual-key'
        with patch.object(remote,'endpoint',return_value=('1.1.1.1',30022)),patch.object(remote,'transport',return_value=t):
            with self.assertRaises(HTTPException) as caught:remote.connect('123',self.login)
        self.assertEqual(caught.exception.status_code,409);t.auth_password.assert_not_called();t.auth_publickey.assert_not_called();t.close.assert_called_once()
    def test_refuses_duplicate_unknown_jobs_and_reconciles_callback(self):
        with main.conn() as c:
            c.execute("INSERT INTO installs VALUES ('job','ready','{}')")
            c.execute("INSERT INTO remote_installs(id,vm_id,status,started) VALUES ('job','123','unknown',0)")
        with patch.object(remote,'connect') as connect:
            with self.assertRaises(HTTPException) as caught:remote.start('123',main.Install(platform_vm_id='123'),self.login,None)
            self.assertEqual(caught.exception.status_code,409);connect.assert_not_called()
        with main.conn() as c:
            c.execute("UPDATE installs SET status='done' WHERE id='job'")
            self.assertEqual(remote.status(c,'job')['status'],'done')
    def test_worker_does_not_record_output_or_claim_success_without_callback(self):
        t=MagicMock();channel=t.open_session.return_value.__enter__.return_value
        channel.recv_ready.side_effect=[True,False];channel.recv.return_value=b'private-password vk_live_secret vless://private'
        channel.recv_stderr_ready.return_value=False;channel.exit_status_ready.return_value=True;channel.recv_exit_status.return_value=0
        with main.conn() as c:
            c.execute("INSERT INTO installs VALUES ('job','ready','{}')")
            c.execute("INSERT INTO remote_installs(id,vm_id,status,started) VALUES ('job','123','running',0)")
        remote.worker('job',t,'fixed-command')
        with main.conn() as c:
            result=remote.status(c,'job');self.assertEqual(result['status'],'unknown');self.assertNotIn('private',json.dumps(result))
        channel.exec_command.assert_called_once_with('fixed-command');t.close.assert_called_once()
    def test_start_uses_only_generated_command_and_clears_credentials(self):
        transport=MagicMock();install=main.Install(platform_vm_id='123');thread=MagicMock()
        with patch.object(remote,'connect',return_value=transport),patch.object(main,'create',return_value={'id':'job','command':'generated-only'}),patch.object(remote.threading,'Thread',return_value=thread) as factory:
            result=remote.start('123',install,self.login,None)
            self.assertEqual(result,{'id':'job','remote':True,'hostname':''})
            self.assertEqual(factory.call_args.kwargs['args'],('job',transport,'generated-only'))
            thread.start.assert_called_once();self.assertEqual(self.login.password,'')
        self.assertNotIn(b'private-password',(main.DATA/'simple.db').read_bytes())

    def test_actual_ssh_auth_root_check_and_generated_command_over_socketpair(self):
        client_sock,server_sock=socket.socketpair();host_key=paramiko.RSAKey.generate(2048);commands=[];server_transport=paramiko.Transport(server_sock);workers=[];channels=[]
        class Server(paramiko.ServerInterface):
            def check_auth_password(self,user,password):
                return paramiko.AUTH_SUCCESSFUL if user=='root' and password=='private-password' else paramiko.AUTH_FAILED
            def get_allowed_auths(self,user):return 'password'
            def check_channel_request(self,kind,chanid):return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            def check_channel_exec_request(self,channel,command):
                commands.append(command.decode())
                def respond():
                    time.sleep(.05)
                    if command==b'id -u':channel.send(b'0\n')
                    else:
                        with main.conn() as c:c.execute("UPDATE installs SET status='done' WHERE id='job'")
                        channel.send(b'private-output-must-not-be-saved')
                    channel.send_exit_status(0);time.sleep(.05);channel.close()
                thread=threading.Thread(target=respond);workers.append(thread);thread.start();return True
        server_transport.add_server_key(host_key)
        def serve():
            server_transport.start_server(server=Server())
            while server_transport.is_active():
                channel=server_transport.accept(.1)
                if channel is not None:channels.append(channel)
        server_thread=threading.Thread(target=serve);server_thread.start()
        self.login.fingerprint=remote.fingerprint(host_key)
        try:
            with patch.object(remote,'endpoint',return_value=('1.1.1.1',30022)),patch.object(remote.socket,'create_connection',return_value=client_sock):
                t=remote.connect('123',self.login)
            with main.conn() as c:
                c.execute("INSERT INTO installs VALUES ('job','ready','{}')")
                c.execute("INSERT INTO remote_installs(id,vm_id,status,started) VALUES ('job','123','running',0)")
            remote.worker('job',t,'fixture-generated-install-command')
            self.assertEqual(commands,['id -u','fixture-generated-install-command'])
            with main.conn() as c:self.assertEqual(remote.status(c,'job')['status'],'done')
            self.assertNotIn(b'private-password',(main.DATA/'simple.db').read_bytes())
            self.assertNotIn(b'private-output',(main.DATA/'simple.db').read_bytes())
        finally:
            client_sock.close();server_transport.close();server_thread.join(timeout=5)
            for thread in workers:thread.join(timeout=5)

if __name__=='__main__':unittest.main()
