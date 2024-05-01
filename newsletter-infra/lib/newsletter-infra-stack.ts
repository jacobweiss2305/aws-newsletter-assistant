import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigatewayv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as apigatewayv2Integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import { HttpMethod } from 'aws-cdk-lib/aws-apigatewayv2';
import * as dotenv from 'dotenv';

dotenv.config();

export class NewsletterInfraStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const requiredEnvVars = [
        'GROQ_API_KEY',
    ];

    for (const envVar of requiredEnvVars) {
        if (typeof process.env[envVar] !== 'string') {
            throw new Error(`${envVar} environment variable is not set`);
        }
    }

    const processStatusTable = new dynamodb.Table(this, "newsletterAgentProcessStatus", {
        partitionKey: { name: "processId", type: dynamodb.AttributeType.STRING },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
    });

    const httpApi = new apigatewayv2.HttpApi(this, "newsletterAgentHttpApi", {
        corsPreflight: {
            allowHeaders: ['Content-Type'],
            allowMethods: [apigatewayv2.CorsHttpMethod.POST, apigatewayv2.CorsHttpMethod.GET],
            allowOrigins: ['*'],
        },
    });

    const newsletterAgentLambda = new lambda.DockerImageFunction(this, "newsletterFinancialAgent", {
        code: lambda.DockerImageCode.fromImageAsset("./lambdas/newsletter-agent"),
        memorySize: 1024,
        timeout: cdk.Duration.seconds(90),
        architecture: lambda.Architecture.ARM_64,
        environment: {
            GROQ_API_KEY: process.env.GROQ_API_KEY as string,
            PROCESS_TABLE: processStatusTable.tableName,
        },
    });
    
    const initiatorLambda = new lambda.DockerImageFunction(this, "newsletterAgentInitiatorFunction", {
        code: lambda.DockerImageCode.fromImageAsset("./lambdas/initiator"),
        memorySize: 1024,
        timeout: cdk.Duration.seconds(30),
        architecture: lambda.Architecture.ARM_64,
        environment: {
            PROCESS_TABLE: processStatusTable.tableName,
            RESEARCH_AGENT_FUNCTION_NAME: newsletterAgentLambda.functionName,
        },
    });

    httpApi.addRoutes({
        path: "/initiate/{type}",
        methods: [apigatewayv2.HttpMethod.POST],
        integration: new apigatewayv2Integrations.HttpLambdaIntegration("InitiatorIntegration", initiatorLambda),
    });

    const statusCheckLambda = new lambda.DockerImageFunction(this, "newsletterAgentStatusCheckFunction", {
        code: lambda.DockerImageCode.fromImageAsset("./lambdas/poller"),
        memorySize: 1024,
        timeout: cdk.Duration.seconds(30),
        architecture: lambda.Architecture.ARM_64,
        environment: {
            PROCESS_TABLE: processStatusTable.tableName
        },
    });

    const lambdaInvokePolicyStatement = new iam.PolicyStatement({
        actions: ['lambda:InvokeFunction'],
        resources: [newsletterAgentLambda.functionArn],
        effect: iam.Effect.ALLOW,
    });

    initiatorLambda.role?.attachInlinePolicy(new iam.Policy(this, 'InvokeLambdaPolicy', {
        statements: [lambdaInvokePolicyStatement],
    }));

    processStatusTable.grantReadWriteData(initiatorLambda);
    processStatusTable.grantReadWriteData(newsletterAgentLambda);
    processStatusTable.grantReadData(statusCheckLambda);
    
    newsletterAgentLambda.grantInvoke(initiatorLambda);

    const statusCheckIntegration = new apigatewayv2Integrations.HttpLambdaIntegration("StatusCheckIntegration", statusCheckLambda);

    httpApi.addRoutes({
        path: "/status/{processId}",
        methods: [apigatewayv2.HttpMethod.GET],
        integration: statusCheckIntegration,
    });

    const newsletterIntegration = new apigatewayv2Integrations.HttpLambdaIntegration("newsletterAgentIntegration", newsletterAgentLambda);

    httpApi.addRoutes({
        path: "/newsletter",
        methods: [HttpMethod.POST],
        integration: newsletterIntegration,
    });

    new cdk.CfnOutput(this, "ProcessStatusTableName", {
        value: processStatusTable.tableName,
    });

    new cdk.CfnOutput(this, "HttpAPIUrl", {
        value: httpApi.url!,
    });


  }
}
