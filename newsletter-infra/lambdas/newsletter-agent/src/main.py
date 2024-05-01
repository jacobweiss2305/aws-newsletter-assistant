import logging
import json
import os

from textwrap import dedent

from phi.llm.groq import Groq
from phi.assistant import Assistant

from duckduckgo_search import DDGS
from phi.tools.newspaper4k import Newspaper4k

import boto3

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    process_id = event["processId"]
    question = event["question"]
    # Update the process status to 'PROCESSING' in DynamoDB
    process_table = dynamodb.Table(os.environ["PROCESS_TABLE"])
    process_table.update_item(
        Key={"processId": process_id},
        UpdateExpression="SET #status = :status",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": "PROCESSING"},
    )

    try:

        def truncate_text(text: str, words: int) -> str:
            return " ".join(text.split()[:words])
        
        news_summary_length = 5000 

        news_results = []

        ddgs = DDGS()
        newspaper_tools = Newspaper4k()
        results = ddgs.news(keywords=question, max_results=5)
        for r in results:
            if "url" in r:
                article_data = newspaper_tools.get_article_data(r["url"])
                if article_data and "text" in article_data:
                    r["text"] = article_data["text"]
                    news_results.append(r)
                    
        logger.info(f"Found {len(news_results)} news articles for {question}")

        logger.info(f"News results: {news_results}")                  

        # Summarizer
        news_summary = ""
        if len(news_results) > 0:
            article_summarizer = Assistant(
                name="Article Summarizer",
                llm=Groq(model="llama3-70b-8192"),
                description="You are a Senior NYT Editor and your task is to summarize a newspaper article.",
                instructions=[
                    "You will be provided with the text from a newspaper article.",
                    "Carefully read the article a prepare a thorough report of key facts and details.",
                    "Your report should be less than 500 words.",
                    "Provide as many details and facts as possible in the summary.",
                    "Your report will be used to generate a final New York Times worthy report.",
                    "REMEMBER: you are writing for the New York Times, so the quality of the report is important.",
                    "Make sure your report is properly formatted and follows the <report_format> provided below.",
                ],
                add_to_system_prompt=dedent(
                    """
            <report_format>
            **Overview:**\n
            {overview of the article}

            **Details:**\n
            {details/facts/main points from the article}

            **Key Takeaways:**\n
            {provide key takeaways from the article}
            </report_format>
            """
                ),
                # This setting tells the LLM to format messages in markdown
                markdown=True,
                add_datetime_to_instructions=True,
            )
            
            for news_result in news_results:
                news_summary += f"### {news_result['title']}\n\n"
                news_summary += f"- Date: {news_result['date']}\n\n"
                news_summary += f"- URL: {news_result['url']}\n\n"
                news_summary += f"#### Introduction\n\n{news_result['body']}\n\n"

                summary = article_summarizer.run(news_result["text"], stream=False)
                summary_length = len(summary.split())
                if summary_length > news_summary_length:
                    summary = truncate_text(summary, news_summary_length)
                    logger.info(f"Truncated summary for {news_result['title']} to {news_summary_length} words.")
                    
                news_summary += "#### Summary\n\n"
                news_summary += summary
                news_summary += "\n\n---\n\n"
                if len(news_summary.split()) > news_summary_length:
                    logger.info(f"Stopping news summary at length: {len(news_summary.split())}")
                    break

        article_draft = ""
        article_draft += f"# Topic: {question}\n\n"
        if news_summary:
            article_draft += "## Summary of News Articles\n\n"
            article_draft += f"This section provides a summary of the news articles about {question}.\n\n"
            article_draft += "<news_summary>\n\n"
            article_draft += f"{news_summary}\n\n"
            article_draft += "</news_summary>\n\n"            

        article_writer = Assistant(
            name="Article Writer",
            llm=Groq(model="llama3-70b-8192"),
            description="You are a Senior NYT Editor and your task is to write a NYT cover story worthy article due tomorrow.",
            instructions=[
                "You will be provided with a topic and pre-processed summaries from junior researchers.",
                "Carefully read the provided information and think about the contents",
                "Then generate a final New York Times worthy article in the <article_format> provided below.",
                "Make your article engaging, informative, and well-structured.",
                "Break the article into sections and provide key takeaways at the end.",
                "Make sure the title is catchy and engaging.",
                "Give the section relevant titles and provide details/facts/processes in each section."
                "REMEMBER: you are writing for the New York Times, so the quality of the article is important.",
            ],
            add_to_system_prompt=dedent(
                """
        <article_format>
        ## Engaging Article Title

        ### Overview
        {give a brief introduction of the article and why the user should read this report}
        {make this section engaging and create a hook for the reader}

        ### Section 1
        {break the article into sections}
        {provide details/facts/processes in this section}

        ... more sections as necessary...

        ### Takeaways
        {provide key takeaways from the article}

        ### References
        - [Title](url)
        - [Title](url)
        - [Title](url)
        </article_format>
        """
            ),
            # This setting tells the LLM to format messages in markdown
            markdown=True,
            add_datetime_to_instructions=True,
        )

        res = article_writer.run(article_draft, stream=False)

        process_table.update_item(
            Key={"processId": process_id},
            UpdateExpression="SET #status = :status, #result = :result",
            ExpressionAttributeNames={"#status": "status", "#result": "result"},
            ExpressionAttributeValues={
                ":status": "COMPLETED",
                ":result": json.dumps(res),
            },
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "processId": process_id,
                    "message": "Process completed successfully",
                    "result": res,
                }
            ),
            "headers": {"Content-Type": "application/json"},
        }
    except Exception as e:
        # Update the process status to 'FAILED' in DynamoDB if an error occurs
        process_table.update_item(
            Key={"processId": process_id},
            UpdateExpression="SET #status = :status, #error = :error",
            ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues={":status": "FAILED", ":error": str(e)},
        )
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "An error occurred during processing"}),
            "headers": {"Content-Type": "application/json"},
        }
