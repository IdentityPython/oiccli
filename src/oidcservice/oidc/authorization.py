import logging

from oidcmsg import oidc
from oidcmsg.oidc import make_openid_request, verified_claim_name
from oidcmsg.time_util import time_sans_frac, utc_time_sans_frac

from oidcservice import rndstr
from oidcservice.exception import ParameterError
from oidcservice.oauth2 import authorization
from oidcservice.oauth2.utils import pick_redirect_uris
from oidcservice.oidc import IDT2REG
from oidcservice.oidc.utils import (construct_request_uri,
                                    request_object_encryption)

__author__ = 'Roland Hedberg'

LOGGER = logging.getLogger(__name__)


class Authorization(authorization.Authorization):
    msg_type = oidc.AuthorizationRequest
    response_cls = oidc.AuthorizationResponse
    error_msg = oidc.ResponseMessage

    def __init__(self, service_context, client_authn_factory=None, conf=None):
        authorization.Authorization.__init__(self, service_context, client_authn_factory, conf=conf)
        self.default_request_args = {'scope': ['openid']}
        self.pre_construct = [self.set_state, pick_redirect_uris,
                              self.oidc_pre_construct]
        self.post_construct = [self.oidc_post_construct]

    def set_state(self, request_args, **kwargs):
        try:
            _state = kwargs['state']
        except KeyError:
            try:
                _state = request_args['state']
            except KeyError:
                _state = ''

        request_args['state'] = self.create_state(
            self.service_context.get('issuer'), _state)
        return request_args, {}

    def update_service_context(self, resp, key='', **kwargs):
        try:
            _idt = resp[verified_claim_name('id_token')]
        except KeyError:
            pass
        else:
            # If there is a verified ID Token then we have to do nonce
            # verification
            try:
                if self.get_state_by_nonce(_idt['nonce']) != key:
                    raise ParameterError('Someone has messed with "nonce"')
            except KeyError:
                raise ValueError('Missing nonce value')

            self.store_sub2state(_idt['sub'], key)

        if 'expires_in' in resp:
            resp['__expires_at'] = time_sans_frac() + int(resp['expires_in'])
        self.store_item(resp.to_json(), 'auth_response', key)

    def oidc_pre_construct(self, request_args=None, post_args=None, **kwargs):
        if request_args is None:
            request_args = {}

        try:
            _rt = request_args["response_type"]
        except KeyError:
            _rt = self.service_context.get('behaviour')['response_types'][0]
            request_args["response_type"] = _rt

        # For OIDC 'openid' is required in scope
        if 'scope' not in request_args:
            request_args['scope'] = self.service_context.get('behaviour').get("scope", ["openid"])
        elif 'openid' not in request_args['scope']:
            request_args['scope'].append('openid')

        # 'code' and/or 'id_token' in response_type means an ID Roken
        # will eventually be returnedm, hence the need for a nonce
        if "code" in _rt or "id_token" in _rt:
            if "nonce" not in request_args:
                request_args["nonce"] = rndstr(32)

        if post_args is None:
            post_args = {}

        for attr in ["request_object_signing_alg", "algorithm", 'sig_kid']:
            try:
                post_args[attr] = kwargs[attr]
            except KeyError:
                pass
            else:
                del kwargs[attr]

        if "request_method" in kwargs:
            if kwargs["request_method"] == "reference":
                post_args['request_param'] = "request_uri"
            else:
                post_args['request_param'] = "request"
            del kwargs["request_method"]

        return request_args, post_args

    def get_request_object_signing_alg(self, **kwargs):
        alg = ''
        for arg in ["request_object_signing_alg", "algorithm"]:
            try:  # Trumps everything
                alg = kwargs[arg]
            except KeyError:
                pass
            else:
                break

        if not alg:
            try:
                alg = self.service_context.get('behaviour')["request_object_signing_alg"]
            except KeyError:  # Use default
                alg = "RS256"
        return alg

    def store_request_on_file(self, req, **kwargs):
        """
        Stores the request parameter in a file.
        :param req: The request
        :param kwargs: Extra keyword arguments
        :return: The URL the OP should use to access the file
        """
        try:
            _webname = self.service_context.get('registration_response')['request_uris'][0]
            filename = self.service_context.filename_from_webname(_webname)
        except KeyError:
            filename, _webname = construct_request_uri(**kwargs)

        fid = open(filename, mode="w")
        fid.write(req)
        fid.close()
        return _webname

    def construct_request_parameter(self, req, request_method, audience=None, expires_in=0,
                                    **kwargs):
        """Construct a request parameter"""
        alg = self.get_request_object_signing_alg(**kwargs)
        kwargs["request_object_signing_alg"] = alg

        if "keys" not in kwargs and alg and alg != "none":
            kwargs["keys"] = self.service_context.keyjar

        _srv_cntx = self.service_context

        # This is the issuer of the JWT, that is me !
        if kwargs.get('issuer') is None:
            kwargs['issuer'] = _srv_cntx.get('client_id')

        if kwargs.get('recv') is None:
            try:
                kwargs['recv'] = _srv_cntx.get('provider_info')['issuer']
            except KeyError:
                kwargs['recv'] = _srv_cntx.get('issuer')

        del kwargs['service']

        if expires_in:
            req['exp'] = utc_time_sans_frac() + int(expires_in)

        _req = make_openid_request(req, **kwargs)

        # Should the request be encrypted
        _req = request_object_encryption(_req, self.service_context,
                                         **kwargs)

        if request_method == "request":
            req["request"] = _req
        else:  # MUST be request_uri
            req["request_uri"] = self.store_request_on_file(_req, **kwargs)

    def oidc_post_construct(self, req, **kwargs):
        """
        Modify the request arguments.

        :param req: The request
        :param kwargs: Extra keyword arguments
        :return: A possibly modified request.
        """
        if 'openid' in req['scope']:
            _response_type = req['response_type'][0]
            if 'id_token' in _response_type or 'code' in _response_type:
                self.store_nonce2state(req['nonce'], req['state'])

        if 'offline_access' in req['scope']:
            if 'prompt' not in req:
                req['prompt'] = 'consent'

        try:
            _request_method = kwargs['request_param']
        except KeyError:
            pass
        else:
            del kwargs['request_param']

            self.construct_request_parameter(req, _request_method, **kwargs)

        self.store_item(req, 'auth_request', req['state'])
        return req

    def gather_verify_arguments(self):
        """
        Need to add some information before running verify()

        :return: dictionary with arguments to the verify call
        """
        _ctx = self.service_context
        kwargs = {
            'iss': _ctx.get('issuer'),
            'keyjar': _ctx.keyjar, 'verify': True,
            'skew': _ctx.clock_skew
        }

        _client_id = _ctx.get('client_id')
        if _client_id:
            kwargs['client_id'] = _client_id

        if 'registration_response' in _ctx:
            _reg_res = _ctx.get('registration_response')
            for attr, param in IDT2REG.items():
                try:
                    kwargs[attr] = _reg_res[param]
                except KeyError:
                    pass

        try:
            kwargs['allow_missing_kid'] = _ctx.allow['missing_kid']
        except KeyError:
            pass

        if 'behaviour' in _ctx:
            _verify_args = _ctx.get('behaviour').get("verify_args")
            if _verify_args:
                kwargs.update(_verify_args)

        return kwargs
