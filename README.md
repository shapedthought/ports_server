# Veeam Ports Server

NOTE: this is not an official Veeam tool. Errors and omisssions are accepted.

This application is a very basic python flask app that fronts a SQLite databased which contains all the ports from Veeam products.

The application works with the frontend application that is hosted the Veeam Architects Site https://www.veeambp.com/

We deciceded to open source both parts of this project so everyone can benefit from it and help improve it.

If you have any suggestions for improvements please send open an issue.

See the frontend project here: https://github.com/shapedthought/portsApp

## Current versions

Last updated: 25-02-2025

| Product              | Version |
| -------------------- | ------- |
| VBR VMware & Hyper-V | 12.3    |
| Agent Management     | 12.3    |
| Explorers            | 12.3    |
| VCC                  | 12.3    |
| VONE                 | 12.3    |
| VSPC                 | 8.1     |
| VRO                  | 7       |
| VB365                | 8       |
| AHV                  | 7       |
| OLVM / RHV           | 6       |
| Proxmox              | 1       |
| VBAWS                | 8       |
| VBAzure              | 7       |
| VBGCP                | 6       |
| Agent for Windows    | 6       |
| Agent for Linux      | 6       |

## How to run

You will need all the packages installed

```
pip install -r requirements.txt
```

Then run the following:

```
gunicorn -w 4 -b 0.0.0.0:8001 ports_server:app
```

You can change the port to whatever you want but the Angular front-end works on 8001.

## Database

The database can be found in allports.db

Schema

- from_port > this is actually the source service
- to_port > target service
- protocol > protocol used
- port > port(s)
- Description > The description
- Product > The associated Veeam product e.g. VBR, VB365, VBAWS

All columns are TEXT as there really isn't any need for number or float values.

Also note that I did very little in the way of cleaning the database so there are some unrully entries which either I or the community can remove and submit a PR.

## Docker

To create a container with the code use the supplied Dockerfile, then the usual build command.

```
docker build yourrepo/portServer:0.1 .
```

Then run:

```
docker run --rm -d -p 8001:8001 portServer:0.1
```

## Scraping

If you are interested in how the data was collected check out the scrape_ports.ipynb where I used Python Pandas to quickly exact the data from each site.

The put all the ports into a sqlite3 database.

## Document versions

All ports are up-to-date as of 03-02-25, includes all ports for v12.3 including Veeam Threat Hunter.

Note that this is NOT an official Veeam tool. Errors and omissions are accepted.

## Future

I would like to add another column that allows for the grouping of certain elements what would normally be implemented together. This will require some changes on the frontend as well.
