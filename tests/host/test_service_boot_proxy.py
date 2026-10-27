"""Guest ABI qualification cannot inherit a host-library or skipped-test pass."""
import copy,hashlib,json,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gateway/tests'))
import service_boot_proxy as proxy

class ServiceBootProxyTests(unittest.TestCase):
    def setUp(self):
        self.pin=json.loads((ROOT/'security/gateway-proxy-dependencies.json').read_bytes())
        self.inputs={'public-source':'e'*64};self.head='a'*40;self.binary='b'*64
        window=self.pin['advisory_review']
        self.parent=dict(schema=1,scope=proxy.evidence.SCOPE,qualified=True,verification_status='complete',
            checkout=dict(clean=True,commit=self.head),inputs=self.inputs,sources=self.pin['sources'],
            binary_sha256=self.binary,libraries={p:'c'*64 for p in proxy.stage_proxy.LIBRARIES.values()},
            source_signatures={name:dict(signer=pin['key']['fingerprint'],sha256=pin['sha256'],changed_archive_refused=True)
                               for name,pin in self.pin['sources'].items()},
            changed_signature_refusals=2,source_version_refusals=2,pcre2_probe_passed=True,pcre2_jit=False,
            pcre2_version=self.pin['sources']['pcre2']['version'])
        self.review=dict(schema=1,scope='operator-approved-minimal-camera-proxy',
            source_policy_sha256=proxy.evidence.digest(ROOT/'security/gateway-proxy-dependencies.json'),
            qualification_sha256=hashlib.sha256(proxy.encoded(self.parent)).hexdigest(),binary_sha256=self.binary,
            reviewed_at=window['reviewed_at_unix'],valid_until=window['valid_until_unix'],
            libraries={name:'c'*64 for name in proxy.stage_proxy.LIBRARIES})
        self.guest={name:'d'*64 for name in proxy.stage_proxy.LIBRARIES}

    def approval(self,parent=None,review=None,boundary=None,guest=None):
        return proxy.approval(proxy.encoded(self.parent if parent is None else parent),
            proxy.encoded(self.review if review is None else review),self.binary,
            self.guest if guest is None else guest,proxy.BOUNDARY if boundary is None else boundary,self.head,self.inputs)

    def test_new_guest_libraries_require_new_boundary_proof_and_preserve_parent_identity(self):
        proof,review=self.approval()
        self.assertEqual(proof['parent_qualification_sha256'],self.review['qualification_sha256'])
        self.assertEqual(proof['parent_review_sha256'],hashlib.sha256(proxy.encoded(self.review)).hexdigest())
        self.assertEqual(review['libraries'],self.guest)
        self.assertEqual(review['qualification_sha256'],hashlib.sha256(proxy.encoded(proof)).hexdigest())
        self.assertNotEqual(review['qualification_sha256'],self.review['qualification_sha256'])
        self.assertEqual(self.review['libraries'],{name:'c'*64 for name in self.guest})
        for flag in ('whole_os_qualified','full_native_advisory_closure','physical_qualified','fips_validated','deployment_permitted'):
            self.assertIs(proof[flag],False)

    def test_skipped_missing_failed_or_logging_unqualified_boundaries_refuse(self):
        for key,value in [('tests',3),('skips',1),('failures',1),('log_discard_qualified',False),('log_configuration_refusals',1),('failures',False),('skips',False)]:
            boundary=dict(proxy.BOUNDARY);boundary[key]=value
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'actual-complete-guest-proxy-boundaries'):
                self.approval(boundary=boundary)
        with self.assertRaises(ValueError):self.approval(boundary={})

    def test_guest_images_cannot_rewrite_the_original_parent_library_approval(self):
        review=copy.deepcopy(self.review);review['libraries']=self.guest
        with self.assertRaisesRegex(ValueError,'parent-proxy-library-binding'):self.approval(review=review)
        for guest in ({},{**self.guest,'extra':'f'*64},{**self.guest,'libc':'not-a-hash'}):
            with self.subTest(guest=guest),self.assertRaises(ValueError):self.approval(guest=guest)

    def test_changed_parent_source_signature_checkout_or_review_refuses(self):
        for field,value in [('inputs',{}),('qualified',False),('checkout',dict(clean=True,commit='f'*40)),
                            ('sources',{}),('source_version_refusals',0)]:
            parent=copy.deepcopy(self.parent);parent[field]=value
            review=copy.deepcopy(self.review);review['qualification_sha256']=hashlib.sha256(proxy.encoded(parent)).hexdigest()
            with self.subTest(field=field),self.assertRaises(ValueError):self.approval(parent=parent,review=review)
        parent=copy.deepcopy(self.parent);parent['source_signatures']['nginx']['changed_archive_refused']=False
        review=copy.deepcopy(self.review);review['qualification_sha256']=hashlib.sha256(proxy.encoded(parent)).hexdigest()
        with self.assertRaisesRegex(ValueError,'parent-proxy-signature-refusal'):self.approval(parent=parent,review=review)
        review=copy.deepcopy(self.review);review['valid_until']+=1
        with self.assertRaisesRegex(ValueError,'current-guest-proxy-review-window'):self.approval(review=review)

if __name__=='__main__':unittest.main()
