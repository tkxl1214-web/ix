"""Integration coverage for the neural model and web workflow."""

import os
import tempfile

_temporary = tempfile.TemporaryDirectory(prefix="local-model-test-")
os.environ["DATA_DIR"] = _temporary.name
os.environ["DATABASE_PATH"] = os.path.join(_temporary.name, "test.sqlite3")
os.environ["MODEL_DIR"] = os.path.join(_temporary.name, "models")
os.environ["SECRET_KEY"] = "test-secret-key-do-not-use-in-production"
os.environ["MODEL_BLOCK_SIZE"] = "32"
os.environ["MODEL_EMBED_DIM"] = "32"
os.environ["MODEL_HEADS"] = "4"
os.environ["MODEL_LAYERS"] = "1"
os.environ["MAX_RESPONSE_TOKENS"] = "8"
os.environ["TRAIN_UPDATES_PER_EPOCH"] = "4"
os.environ["AUTO_TRAINING_CHECK_SECONDS"] = "10"

import time
import unittest
from unittest.mock import patch

import torch
from fastapi.testclient import TestClient

from backend.main import app
from backend import main as application
from backend import database
from backend.model import TinyTransformer
from backend.tokenizer import ByteTokenizer


def create_test_user(email, password, role="user"):
    password_hash=application.hash_password(password)
    with database.transaction() as connection:
        cursor=connection.execute("INSERT INTO users(email,password_hash,role) VALUES(?,?,?)",(email,password_hash,role))
        user_id=cursor.lastrowid
    application.seed_starter_corpus(user_id)
    return {"token":application.issue_token(user_id,role),"id":user_id}


class NeuralModelTests(unittest.TestCase):
    def test_tokenizer_round_trip_and_special_tokens(self):
        tokenizer = ByteTokenizer()
        source = "안녕하세요 · local"
        tokens = tokenizer.encode(source)
        self.assertEqual(tokens[0], tokenizer.bos_id)
        self.assertEqual(tokens[-1], tokenizer.eos_id)
        self.assertEqual(tokenizer.decode(tokens), source)

    def test_forward_backward_weights_checkpoint_and_generation(self):
        config = {"vocab_size":258,"block_size":16,"n_embd":16,"n_head":4,"n_layer":1,"dropout":0.0}
        model = TinyTransformer(config)
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
        original = model.token_embedding.weight.detach().clone()
        tokens = torch.randint(0, 258, (2, 8))
        _, loss = model(tokens[:, :-1], tokens[:, 1:])
        loss.backward(); optimizer.step()
        self.assertFalse(torch.equal(original, model.token_embedding.weight.detach()))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "checkpoint.pt")
            torch.save({"config":config,"model_state":model.state_dict()},path)
            restored = TinyTransformer(config)
            restored.load_state_dict(torch.load(path,map_location="cpu",weights_only=False)["model_state"])
            self.assertTrue(torch.equal(model.token_embedding.weight,restored.token_embedding.weight))
            generated = restored.generate_one(torch.tensor([[256,65]]),temperature=1,top_k=4)
            self.assertGreaterEqual(generated,0); self.assertLess(generated,258)


class WebWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        admin = create_test_user("admin@example.com","admin-password-123","admin")
        cls.admin_token = admin["token"]
        cls.admin_headers = {"Authorization":f"Bearer {cls.admin_token}"}
        auto = cls.client.get('/api/training/auto',headers=cls.admin_headers)
        if auto.status_code != 200 or not auto.json()["enabled"]:
            raise AssertionError(f"automatic learning should be enabled on first startup: {auto.text}")
        dashboard = cls.client.get('/api/dashboard',headers=cls.admin_headers)
        if dashboard.status_code != 200 or dashboard.json()["approved_data_count"] < 20 or not dashboard.json()["learning_materials"]:
            raise AssertionError(f"starter learning materials were not seeded: {dashboard.text}")
        # Keep the integration suite deterministic, then exercise the automatic scheduler explicitly below.
        disabled = cls.client.put('/api/training/auto',headers=cls.admin_headers,json={"enabled":False,"min_new_items":1})
        if disabled.status_code != 200:
            raise AssertionError(disabled.text)

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None,None,None)
        for process in list(application._processes.values()):
            try: process.wait(timeout=10)
            except Exception:
                try: process.terminate(); process.wait(timeout=5)
                except Exception: pass

    def test_auth_memory_conversation_and_user_isolation(self):
        rejected = self.client.post('/api/auth/register',json={"email":"no-consent@example.com","password":"user-password-123"})
        self.assertEqual(rejected.status_code,410)
        user = create_test_user("user@example.com","user-password-123")
        headers={"Authorization":f"Bearer {user['token']}"}
        memory=self.client.post('/api/memories',headers=headers,json={"content":"저는 간결한 한국어 답변을 선호합니다.","category":"선호"})
        self.assertEqual(memory.status_code,200)
        memory_id=memory.json()["id"]
        edited=self.client.put(f'/api/memories/{memory_id}',headers=headers,json={"content":"저는 간결한 한국어 설명을 선호합니다.","category":"응답 선호"})
        self.assertEqual(edited.status_code,200)
        uploaded=self.client.post('/api/datasets/upload',headers=headers,files={"file":("notes.txt","This document contains approved learning text for the private model.".encode(),"text/plain")},data={"consent":"true"})
        self.assertEqual(uploaded.status_code,200,uploaded.text)
        conversation=self.client.post('/api/conversations',headers=headers).json()["id"]
        self.assertEqual(self.client.get(f'/api/conversations/{conversation}/messages',headers=self.admin_headers).status_code,404)
        self.assertEqual(self.client.get('/api/memories',headers=self.admin_headers).json(),[])
        self.assertEqual(self.client.delete(f'/api/memories/{memory_id}',headers=headers).status_code,200)
        deleted=self.client.delete('/api/account/data',headers=headers)
        self.assertEqual(deleted.status_code,200)

    def test_google_oauth_state_and_cookie_session(self):
        state = "state-value-for-test"
        verifier = "v" * 48
        signed = application.sign_google_state(state, verifier)
        verified = application.verify_google_state(signed, state)
        self.assertEqual(verified["verifier"], verifier)
        self.assertIsNone(application.verify_google_state(signed, "wrong-state"))
        self.assertEqual(self.client.get('/api/auth/google/status').json(), {"enabled":False})

        self.client.cookies.set("ai_session", self.admin_token)
        self.assertEqual(self.client.get('/api/auth/me').status_code,200)
        self.assertEqual(self.client.post('/api/auth/logout').status_code,200)
        self.client.cookies.delete("ai_session")
        self.assertEqual(self.client.get('/api/auth/me').status_code,401)

    def test_google_signup_ticket_requires_policy_consent(self):
        old_client_id,old_client_secret,old_redirect=application.GOOGLE_CLIENT_ID,application.GOOGLE_CLIENT_SECRET,application.GOOGLE_REDIRECT_URI
        application.GOOGLE_CLIENT_ID="test-client"
        application.GOOGLE_CLIENT_SECRET="test-secret"
        application.GOOGLE_REDIRECT_URI="http://testserver/api/auth/google/callback"
        state="google-signup-state"
        verifier="v"*48
        self.client.cookies.set("google_oauth_state",application.sign_google_state(state,verifier))
        profile={"email":"google-new@example.com","sub":"google-subject-test","name":"구글 사용자","email_verified":True}
        try:
            with patch.object(application,"exchange_google_code",return_value=profile):
                callback=self.client.get('/api/auth/google/callback',params={"code":"authorization-code","state":state},follow_redirects=False)
            self.assertEqual(callback.status_code,303,callback.text)
            self.assertEqual(callback.headers["location"],"/?google_signup=1")
        finally:
            application.GOOGLE_CLIENT_ID,application.GOOGLE_CLIENT_SECRET,application.GOOGLE_REDIRECT_URI=old_client_id,old_client_secret,old_redirect
        status=self.client.get('/api/auth/google/registration/status')
        self.assertEqual(status.status_code,200,status.text)
        self.assertEqual(status.json()["email"],"google-new@example.com")
        payload={"display_name":"구글 사용자","age":26,"accepted_terms":True,"accepted_privacy":True,"accepted_youth":False}
        self.assertEqual(self.client.post('/api/auth/google/register',json=payload).status_code,422)
        payload["accepted_youth"]=True
        created=self.client.post('/api/auth/google/register',json=payload)
        self.assertEqual(created.status_code,200,created.text)
        headers={"Authorization":"Bearer "+created.json()["token"]}
        profile=self.client.get('/api/auth/me',headers=headers).json()
        self.assertEqual(profile["email"],"google-new@example.com")
        self.assertEqual(profile["age"],26)
        with database.connect() as connection:
            row=connection.execute("SELECT google_sub,accepted_terms_at,accepted_privacy_at,accepted_youth_at FROM users WHERE email=?",("google-new@example.com",)).fetchone()
        self.assertEqual(row["google_sub"],"google-subject-test")
        self.assertTrue(all(row[field] for field in ("accepted_terms_at","accepted_privacy_at","accepted_youth_at")))

    def test_training_consent_review_worker_checkpoint_and_rollback(self):
        # The response content repeats so the byte model can measurably learn the held-out tail.
        text=("The small local transformer learns from approved examples. " * 80)
        item=self.client.post('/api/datasets',headers=self.admin_headers,json={"content":text,"consent":True})
        self.assertEqual(item.status_code,200,item.text)
        dataset_id=item.json()["id"]
        self.assertEqual(self.client.post('/api/datasets',headers=self.admin_headers,json={"content":text,"consent":True}).status_code,409)
        self.assertEqual(self.client.post(f'/api/datasets/{dataset_id}/review?decision=approve',headers={"Authorization":"Bearer " + self.client.post('/api/auth/login',json={"email":"admin@example.com","password":"admin-password-123"}).json()["token"]}).status_code,200)
        start=self.client.post('/api/training/start',headers=self.admin_headers,json={"epochs":1})
        self.assertEqual(start.status_code,200,start.text)
        job_id=start.json()["id"]
        deadline=time.time()+90
        job=None
        while time.time()<deadline:
            response=self.client.get(f'/api/training/jobs/{job_id}',headers=self.admin_headers)
            self.assertEqual(response.status_code,200,response.text)
            job=response.json()["job"]
            if job["status"] in {"completed","failed","cancelled"}: break
            time.sleep(.25)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"],"completed",job.get("error"))
        self.assertGreaterEqual(len(self.client.get(f'/api/training/jobs/{job_id}',headers=self.admin_headers).json()["metrics"]),1)
        versions=self.client.get('/api/models/versions',headers=self.admin_headers).json()
        self.assertGreaterEqual(len(versions),1)
        more_text=("Approved material teaches the same compact local network. " * 80)
        more_item=self.client.post('/api/datasets',headers=self.admin_headers,json={"content":more_text,"consent":True})
        self.assertEqual(more_item.status_code,200,more_item.text)
        more_id=more_item.json()["id"]
        self.assertEqual(self.client.post(f'/api/datasets/{more_id}/review?decision=approve',headers=self.admin_headers).status_code,200)
        automatic=self.client.put('/api/training/auto',headers=self.admin_headers,json={"enabled":True,"min_new_items":1})
        self.assertEqual(automatic.status_code,200,automatic.text)
        self.assertTrue(self.client.get('/api/training/auto',headers=self.admin_headers).json()["enabled"])
        auto_job=None
        deadline=time.time()+35
        while time.time()<deadline:
            jobs=self.client.get('/api/training/jobs',headers=self.admin_headers).json()
            candidates=[item for item in jobs if item["id"]!=job_id]
            if candidates:
                auto_job=candidates[0]
                if auto_job["status"] in {"completed","failed","cancelled"}: break
            time.sleep(.5)
        self.client.put('/api/training/auto',headers=self.admin_headers,json={"enabled":False,"min_new_items":2})
        self.assertIsNotNone(auto_job,"automatic scheduler did not create a job")
        self.assertEqual(auto_job["status"],"completed",auto_job.get("error"))

        long_job=self.client.post('/api/training/start',headers=self.admin_headers,json={"epochs":50})
        self.assertEqual(long_job.status_code,200,long_job.text)
        long_id=long_job.json()["id"]
        deadline=time.time()+20
        while time.time()<deadline:
            response=self.client.get(f'/api/training/jobs/{long_id}',headers=self.admin_headers)
            self.assertEqual(response.status_code,200,response.text)
            status=response.json()["job"]["status"]
            if status=="running": break
            time.sleep(.35)
        self.assertEqual(status,"running")
        self.assertEqual(self.client.post(f'/api/training/jobs/{long_id}/pause',headers=self.admin_headers).status_code,200)
        deadline=time.time()+20
        while time.time()<deadline:
            response=self.client.get(f'/api/training/jobs/{long_id}',headers=self.admin_headers)
            self.assertEqual(response.status_code,200,response.text)
            status=response.json()["job"]["status"]
            if status=="paused": break
            time.sleep(.35)
        self.assertEqual(status,"paused")
        self.assertEqual(self.client.post(f'/api/training/jobs/{long_id}/resume',headers=self.admin_headers).status_code,200)
        deadline=time.time()+20
        while time.time()<deadline:
            response=self.client.get(f'/api/training/jobs/{long_id}',headers=self.admin_headers)
            self.assertEqual(response.status_code,200,response.text)
            status=response.json()["job"]["status"]
            if status=="running": break
            time.sleep(.35)
        self.assertEqual(status,"running")
        self.assertEqual(self.client.post(f'/api/training/jobs/{long_id}/cancel',headers=self.admin_headers).status_code,200)
        deadline=time.time()+20
        while time.time()<deadline:
            response=self.client.get(f'/api/training/jobs/{long_id}',headers=self.admin_headers)
            self.assertEqual(response.status_code,200,response.text)
            status=response.json()["job"]["status"]
            if status=="cancelled": break
            time.sleep(.35)
        self.assertEqual(status,"cancelled")
        rollback=self.client.post('/api/models/rollback/v0000',headers=self.admin_headers)
        self.assertEqual(rollback.status_code,200,rollback.text)

    def test_streamed_chat_and_frontend(self):
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertIn('viewport',response.text)
        self.assertIn('지금 바로 첫 질문을 시작해볼까요?',response.text)
        self.assertIn('Google로 로그인',response.text)
        self.assertIn('Google로 가입하기',response.text)
        self.assertIn('/assets/ixaistudio-logo.svg',response.text)
        self.assertEqual(self.client.get('/assets/ixaistudio-logo.svg').status_code,200)
        self.assertEqual(self.client.post('/api/auth/register',json={}).status_code,410)
        self.assertEqual(self.client.get('/terms').status_code,200)
        self.assertEqual(self.client.get('/youth').status_code,200)
        stream=self.client.post('/api/chat',headers=self.admin_headers,json={"content":"Hello model","conversation_id":None})
        self.assertEqual(stream.status_code,200,stream.text)
        self.assertIn('event: meta',stream.text)
        self.assertIn('event: done',stream.text)
        conversations=self.client.get('/api/conversations',headers=self.admin_headers).json()
        messages=self.client.get(f"/api/conversations/{conversations[0]['id']}/messages",headers=self.admin_headers).json()
        assistant=next(item for item in messages if item['role']=='assistant')
        feedback=self.client.post('/api/feedback',headers=self.admin_headers,json={"message_id":assistant['id'],"rating":1,"approve_for_training":True})
        self.assertEqual(feedback.status_code,200,feedback.text)
        self.assertTrue(feedback.json()["submitted_for_review"])
        health=self.client.get('/api/health').json()
        self.assertFalse(health["ai_api"])


if __name__ == '__main__':
    unittest.main()
