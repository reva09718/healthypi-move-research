"""Generate a self-contained CloudFormation template. Does not call AWS."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ref = lambda name: {'Ref': name}
att = lambda name, field: {'Fn::GetAtt':[name,field]}
sub = lambda value: {'Fn::Sub':value}
code = (ROOT/'backend.py').read_text(encoding='utf-8')
issuer = sub('https://cognito-idp.${AWS::Region}.amazonaws.com/${UserPool}')
resources = {
    'Bucket': {'Type':'AWS::S3::Bucket','DeletionPolicy':'Retain','UpdateReplacePolicy':'Retain','Properties':{
        'PublicAccessBlockConfiguration':{x:True for x in ['BlockPublicAcls','BlockPublicPolicy','IgnorePublicAcls','RestrictPublicBuckets']},
        'OwnershipControls':{'Rules':[{'ObjectOwnership':'BucketOwnerEnforced'}]},
        'VersioningConfiguration':{'Status':'Enabled'},
        'LifecycleConfiguration':{'Rules':[{'Id':'RemoveOldVersions','Status':'Enabled','NoncurrentVersionExpiration':{'NoncurrentDays':7},'AbortIncompleteMultipartUpload':{'DaysAfterInitiation':1}}]},
        'BucketEncryption':{'ServerSideEncryptionConfiguration':[{'ServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]}}},
    'BucketPolicy': {'Type':'AWS::S3::BucketPolicy','Properties':{'Bucket':ref('Bucket'),'PolicyDocument':{'Version':'2012-10-17','Statement':[
        {'Effect':'Deny','Principal':'*','Action':'s3:*','Resource':[att('Bucket','Arn'),sub('${Bucket.Arn}/*')],
         'Condition':{'Bool':{'aws:SecureTransport':'false'}}}]}}},
    'Table': {'Type':'AWS::DynamoDB::Table','DeletionPolicy':'Retain','UpdateReplacePolicy':'Retain','Properties':{
        'BillingMode':'PAY_PER_REQUEST','AttributeDefinitions':[{'AttributeName':k,'AttributeType':'S'} for k in ['pk','sk']],
        'KeySchema':[{'AttributeName':'pk','KeyType':'HASH'},{'AttributeName':'sk','KeyType':'RANGE'}],
        'SSESpecification':{'SSEEnabled':True},'PointInTimeRecoverySpecification':{'PointInTimeRecoveryEnabled':True}}},
    'SignupRole': {'Type':'AWS::IAM::Role','Properties':{'AssumeRolePolicyDocument':{'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Principal':{'Service':'lambda.amazonaws.com'},'Action':'sts:AssumeRole'}]},
        'Policies':[{'PolicyName':'Logs','PolicyDocument':{'Version':'2012-10-17','Statement':[
            {'Effect':'Allow','Action':['logs:CreateLogStream','logs:PutLogEvents'],'Resource':sub('arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${AWS::StackName}-signup:*')}]}}]}},
    'SignupLog': {'Type':'AWS::Logs::LogGroup','Properties':{'LogGroupName':sub('/aws/lambda/${AWS::StackName}-signup'),'RetentionInDays':14}},
    'Signup': {'Type':'AWS::Lambda::Function','DependsOn':['SignupLog'],'Properties':{'FunctionName':sub('${AWS::StackName}-signup'),
        'Runtime':'python3.12','Handler':'index.signup','Timeout':10,'Role':att('SignupRole','Arn'),'Code':{'ZipFile':code},
        'Environment':{'Variables':{'ALLOWED_EMAILS':ref('AllowedSignupEmails')}}}},
    'SignupPermission': {'Type':'AWS::Lambda::Permission','Properties':{'FunctionName':ref('Signup'),'Action':'lambda:InvokeFunction',
        'Principal':'cognito-idp.amazonaws.com','SourceAccount':ref('AWS::AccountId'),'SourceArn':att('UserPool','Arn')}},
    'UserPool': {'Type':'AWS::Cognito::UserPool','Properties':{'UserPoolName':sub('${AWS::StackName}-users'),
        'UsernameAttributes':['email'],'UsernameConfiguration':{'CaseSensitive':False},'AutoVerifiedAttributes':['email'],
        'AccountRecoverySetting':{'RecoveryMechanisms':[{'Name':'verified_email','Priority':1}]},
        'Policies':{'PasswordPolicy':{'MinimumLength':12,'RequireLowercase':True,'RequireUppercase':True,'RequireNumbers':True,'RequireSymbols':True}},
        'LambdaConfig':{'PreSignUp':att('Signup','Arn')}}},
    'Client': {'Type':'AWS::Cognito::UserPoolClient','Properties':{'UserPoolId':ref('UserPool'),'GenerateSecret':False,
        'ExplicitAuthFlows':['ALLOW_USER_PASSWORD_AUTH','ALLOW_REFRESH_TOKEN_AUTH'],
        'AccessTokenValidity':60,'IdTokenValidity':60,'RefreshTokenValidity':7,
        'TokenValidityUnits':{'AccessToken':'minutes','IdToken':'minutes','RefreshToken':'days'},
        'PreventUserExistenceErrors':'ENABLED','EnableTokenRevocation':True,
        'ReadAttributes':['email','email_verified','sub'],'WriteAttributes':['email']}},
    'ApiRole': {'Type':'AWS::IAM::Role','Properties':{'AssumeRolePolicyDocument':{'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Principal':{'Service':'lambda.amazonaws.com'},'Action':'sts:AssumeRole'}]},
        'Policies':[{'PolicyName':'MobileStorage','PolicyDocument':{'Version':'2012-10-17','Statement':[
            {'Effect':'Allow','Action':['s3:PutObject','s3:GetObject'],'Resource':sub('${Bucket.Arn}/users/*')},
            {'Effect':'Allow','Action':['dynamodb:GetItem','dynamodb:PutItem','dynamodb:UpdateItem','dynamodb:Query','dynamodb:ConditionCheckItem'],'Resource':att('Table','Arn')},
            {'Effect':'Allow','Action':['logs:CreateLogStream','logs:PutLogEvents'],'Resource':sub('arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${AWS::StackName}-api:*')}]}}]}},
    'ApiLog': {'Type':'AWS::Logs::LogGroup','Properties':{'LogGroupName':sub('/aws/lambda/${AWS::StackName}-api'),'RetentionInDays':14}},
    'Function': {'Type':'AWS::Lambda::Function','DependsOn':['ApiLog'],'Properties':{'FunctionName':sub('${AWS::StackName}-api'),
        'Runtime':'python3.12','Handler':'index.handler','Timeout':29,'MemorySize':256,'Role':att('ApiRole','Arn'),'Code':{'ZipFile':code},
        'Environment':{'Variables':{'BUCKET':ref('Bucket'),'TABLE':ref('Table'),'ISSUER':issuer,'CLIENT_ID':ref('Client')}}}},
    'Api': {'Type':'AWS::ApiGatewayV2::Api','Properties':{'Name':sub('${AWS::StackName}-api'),'ProtocolType':'HTTP'}},
    'Authorizer': {'Type':'AWS::ApiGatewayV2::Authorizer','Properties':{'ApiId':ref('Api'),'AuthorizerType':'JWT','Name':'CognitoUsers',
        'IdentitySource':['$request.header.Authorization'],'JwtConfiguration':{'Audience':[ref('Client')],'Issuer':issuer}}},
    'Integration': {'Type':'AWS::ApiGatewayV2::Integration','Properties':{'ApiId':ref('Api'),'IntegrationType':'AWS_PROXY',
        'IntegrationUri':att('Function','Arn'),'PayloadFormatVersion':'2.0','TimeoutInMillis':29000}},
    'Route': {'Type':'AWS::ApiGatewayV2::Route','Properties':{'ApiId':ref('Api'),'RouteKey':'ANY /{proxy+}',
        'Target':{'Fn::Join':['/', ['integrations',ref('Integration')]]},'AuthorizationType':'JWT','AuthorizerId':ref('Authorizer'),
        'AuthorizationScopes':['aws.cognito.signin.user.admin']}},
    'Stage': {'Type':'AWS::ApiGatewayV2::Stage','Properties':{'ApiId':ref('Api'),'StageName':'$default','AutoDeploy':True,
        'DefaultRouteSettings':{'ThrottlingBurstLimit':60,'ThrottlingRateLimit':30}}},
    'ApiPermission': {'Type':'AWS::Lambda::Permission','Properties':{'FunctionName':ref('Function'),'Action':'lambda:InvokeFunction',
        'Principal':'apigateway.amazonaws.com','SourceArn':sub('arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${Api}/*')}}
}
template={'AWSTemplateFormatVersion':'2010-09-09','Description':'HealthyPi Move private mobile recording backup (Sydney)',
    'Parameters':{'AllowedSignupEmails':{'Type':'String','Default':'','Description':'Comma-separated project member emails. Empty denies all self-signups.'}},
    'Rules':{'SydneyOnly':{'Assertions':[{'Assert':{'Fn::Equals':[ref('AWS::Region'),'ap-southeast-2']},'AssertDescription':'Deploy in Sydney ap-southeast-2.'}]}},
    'Resources':resources,'Outputs':{
        'AppConfig':{'Value':sub('{"region":"${AWS::Region}","userPoolId":"${UserPool}","clientId":"${Client}","apiBaseUrl":"https://${Api}.execute-api.${AWS::Region}.amazonaws.com"}')},
        'BucketName':{'Value':ref('Bucket')},'TableName':{'Value':ref('Table')}}}
out=ROOT/'template.generated.json'
out.write_text(json.dumps(template,separators=(',',':')),encoding='utf-8')
assert out.stat().st_size < 51200, 'Template must fit direct CloudFormation deployment'
print(f'Generated {out.name}: {out.stat().st_size} bytes (no AWS calls)')
